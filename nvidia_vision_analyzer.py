#!/usr/bin/env python3
"""
NVIDIA Nemotron Vision Analyzer for Stock Charts
使用 NVIDIA 免費 API (Nemotron 3 Ultra) 進行 K 線圖視覺分析
"""
import os
import json
import base64
import io
import random
from pathlib import Path
from typing import Dict, Any, Optional, List

import mplfinance as mpf
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from openai import OpenAI


class ChartGenerator:
    """專業 K 線圖生成器"""
    
    def __init__(self, dpi: int = 120):
        self.dpi = dpi
        self.mpf_style = mpf.make_mpf_style(
            base_mpf_style='charles',
            rc={'font.size': 9, 'figure.figsize': (14, 8)}
        )
    
    def generate(self, df: pd.DataFrame, symbol: str, markers: Dict = None, bars: int = 100) -> bytes:
        """生成 K 線圖，返回 PNG bytes"""
        plot_df = df.copy()
        plot_df.index = pd.to_datetime(plot_df['Date'])
        plot_df = plot_df[['Open','High','Low','Close','Volume']].tail(bars)
        
        apds = []
        for period, color, width in [(20, '#2196F3', 1), (50, '#FF9800', 1), (200, '#9C27B0', 1.5)]:
            plot_df[f'MA{period}'] = plot_df['Close'].rolling(period).mean()
            apds.append(mpf.make_addplot(plot_df[f'MA{period}'], color=color, width=width, alpha=0.8))
        
        if markers:
            for name, idx in markers.items():
                if idx is not None and idx < len(plot_df):
                    price = plot_df['Low'].iloc[idx] * 0.993 if 'buy' in name.lower() else plot_df['High'].iloc[idx] * 1.007
                    ms = pd.Series(index=plot_df.index, dtype=float)
                    ms.iloc[idx] = price
                    color = '#4CAF50' if 'buy' in name.lower() or 'confirm' in name.lower() else '#F44336'
                    apds.append(mpf.make_addplot(ms, type='scatter', markersize=100, marker='^' if color=='#4CAF50' else 'v', color=color))
        
        buf = io.BytesIO()
        mpf.plot(plot_df, type='candle', style=self.mpf_style, volume=True, addplot=apds,
                 title=f'{symbol} - Visual Analysis', ylabel='Price', ylabel_lower='Vol',
                 savefig=dict(fname=buf, format='png', dpi=self.dpi, bbox_inches='tight'))
        plt.close()
        buf.seek(0)
        return buf.read()


class NvidiaVisionAnalyzer:
    """NVIDIA Nemotron 視覺分析器
    
    支援模型：
    - nvidia/nemotron-3-ultra (最強推理)
    - nvidia/nemotron-4-340b (新一代)
    - nvidia/llama-3.1-nemotron-70b-instruct (較輕量)
    
    API 端點: https://integrate.api.nvidia.com/v1
    """
    
    def __init__(self, api_key: str, model: str = "nvidia/nemotron-3-ultra"):
        self.client = OpenAI(api_key=api_key, base_url="https://integrate.api.nvidia.com/v1")
        self.model = model
    
    def analyze(self, img_bytes: bytes, symbol: str, ctx: Dict) -> Dict:
        """分析 K 線圖"""
        img_b64 = base64.b64encode(img_bytes).decode()
        prompt = self._prompt(symbol, ctx)
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}}
            ]}],
            max_tokens=1200, temperature=0.1, top_p=0.9, seed=42
        )
        return self._parse(resp.choices[0].message.content)
    
    def _prompt(self, symbol: str, ctx: Dict) -> str:
        return f"""你是 CMT Level 3 技術分析師，分析 {symbol} 的 K 線圖。

【量化背景】
形態: {ctx.get('pattern')} | 區間: {ctx.get('zone')} | 確認日: {ctx.get('confirm_date')} | 回調日: {ctx.get('pullback_date')}
量化評分: {ctx.get('score')}/100 | 0.618: {ctx.get('fib618')} | 量能: {ctx.get('volume_feature')} | 減速: {ctx.get('slowdown_feature')}

【請驗證並評分 0-100】
1. 支撐/阻力有效性 2. 量價確認 3. K 線形態 4. 均線系統 5. 趨勢結構 6. 風險點

【只輸出 JSON】
{{
  "support_resistance": {{"valid": true, "score": 85, "evidence": "..."}},
  "volume_confirm": {{"valid": true, "score": 78, "evidence": "..."}},
  "candle_pattern": {{"detected": "錘頭線", "score": 82}},
  "ma_system": {{"arrangement": "bullish", "score": 88}},
  "trend_structure": {{"score": 80}},
  "risk_flags": ["..."],
  "visual_score": 82,
  "conviction": "HIGH",
  "summary": "...",
  "action": "BUY_ON_BREAKOUT",
  "entry_zone": "152-153",
  "stop_loss": "148",
  "target_1": "165"
}}"""
    
    def _parse(self, content: str) -> Dict:
        content = content.strip()
        if '```json' in content: content = content.split('```json')[1].split('```')[0]
        elif '```' in content: content = content.split('```')[1].split('```')[0]
        try: return json.loads(content.strip())
        except: return {"error": "parse failed", "raw": content[:500]}


def analyze_top10(results: list, stage2_data: dict, artifact_dir: str, api_key: str) -> list:
    """分析前 10 名候選"""
    chart_gen = ChartGenerator(dpi=100)
    vision = NvidiaVisionAnalyzer(api_key, "nvidia/nemotron-3-ultra")
    save_dir = Path(artifact_dir) / 'charts'
    save_dir.mkdir(exist_ok=True)
    
    for i, cand in enumerate(results[:10]):
        ys = cand['symbol'].replace('.', '-')
        if ys not in stage2_data:
            continue
        df = stage2_data[ys]
        markers = {}
        for k, label in [('confirm_date', 'confirm_buy'), ('pullback_date', 'pullback')]:
            if k in cand:
                idx = df.index[df['Date'] == cand[k]].tolist()
                if idx: markers[label] = idx[0]
        
        try:
            img = chart_gen.generate(df, cand['symbol'], markers, bars=100)
            (save_dir / f"{cand['symbol']}_chart.png").write_bytes(img)
            
            ctx = {k: cand.get(k) for k in ['pattern','zone','confirm_date','pullback_date','score','fib618','volume_feature','slowdown_feature']}
            v = vision.analyze(img, cand['symbol'], ctx)
            
            tech = cand.get('score', 0)
            vis = v.get('visual_score', 0)
            cand['visual_analysis'] = v
            cand['final_score'] = round(tech * 0.6 + vis * 0.4, 1)
            print(f"  [{i+1}] {cand['symbol']}: tech={tech} visual={vis} final={cand['final_score']} action={v.get('action')}")
        except Exception as e:
            print(f"  [{i+1}] {cand['symbol']}: ERROR - {e}")
            cand['visual_analysis'] = {"error": str(e)}
            cand['final_score'] = cand.get('score', 0)
    
    results.sort(key=lambda x: x.get('final_score', x['score']), reverse=True)
    return results