"""
LaTeX 公式渲染模块
"""
import re
import io
import hashlib
from pathlib import Path
from typing import List, Dict, Any

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from astrbot.api import logger
from astrbot.api.message_components import Plain, Image
from astrbot.api.event import AstrMessageEvent


class LatexRenderer:
    """LaTeX 公式渲染器"""
    
    def __init__(self, img_dir: Path):
        self.img_dir = img_dir
        self.img_dir.mkdir(parents=True, exist_ok=True)
    
    def render_latex(self, formula: str, display_mode: bool = False) -> bytes:
        """渲染 LaTeX 公式为图片"""
        try:
            formula = formula.strip()
            
            plt.figure(figsize=(0.01, 0.01))
            if display_mode:
                tex = f"$${formula}$$"
            else:
                tex = f"${formula}$"
            
            plt.text(0.5, 0.5, tex, fontsize=12, ha='center', va='center')
            plt.axis('off')
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=150, bbox_inches='tight', pad_inches=0.1)
            plt.close()
            buf.seek(0)
            return buf.read()
        except Exception as e:
            logger.error(f"LaTeX 渲染失败: {e}")
            plt.figure(figsize=(2, 1))
            plt.text(0.5, 0.5, "[公式渲染失败]", fontsize=10, ha='center', va='center')
            plt.axis('off')
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
            plt.close()
            buf.seek(0)
            return buf.read()
    
    def process_latex(self, text: str) -> List[Dict[str, Any]]:
        """处理文本中的 LaTeX 公式"""
        pattern = r'(\$\$.*?\$\$|\$.*?\$)'
        parts = re.split(pattern, text, flags=re.DOTALL)
        result = []
        
        for idx, part in enumerate(parts):
            if not part:
                continue
            
            if part.startswith('$$') and part.endswith('$$'):
                formula = part[2:-2].strip()
                try:
                    img_data = self.render_latex(formula, display_mode=True)
                    result.append({'type': 'image', 'data': img_data, 'index': idx})
                except Exception as e:
                    logger.error(f"渲染行间公式失败: {e}")
                    result.append({'type': 'text', 'text': part, 'index': idx})
            elif part.startswith('$') and part.endswith('$'):
                formula = part[1:-1].strip()
                try:
                    img_data = self.render_latex(formula, display_mode=False)
                    result.append({'type': 'image', 'data': img_data, 'index': idx})
                except Exception as e:
                    logger.error(f"渲染行内公式失败: {e}")
                    result.append({'type': 'text', 'text': part, 'index': idx})
            else:
                if part.strip():
                    result.append({'type': 'text', 'text': part, 'index': idx})
        
        result.sort(key=lambda x: x['index'])
        return result
    
    async def send_with_latex(self, event: AstrMessageEvent, text: str):
        """发送带 LaTeX 公式的消息"""
        try:
            segments = self.process_latex(text)
            chain = []
            
            for seg in segments:
                if seg['type'] == 'text':
                    chain.append(Plain(seg['text']))
                else:
                    img_hash = hashlib.md5(seg['data']).hexdigest()[:12]
                    img_path = self.img_dir / f"latex_{img_hash}.png"
                    
                    if not img_path.exists():
                        with open(img_path, 'wb') as f:
                            f.write(seg['data'])
                    chain.append(Image.fromFileSystem(str(img_path)))
            
            await event.send(event.chain_result(chain))
        except Exception as e:
            logger.error(f"发送 LaTeX 消息失败: {e}")
            await event.send(event.plain_result(text))
    
    def cleanup(self):
        """清理渲染的图片文件"""
        try:
            if self.img_dir.exists():
                for f in self.img_dir.glob("*.png"):
                    try:
                        f.unlink()
                    except Exception as e:
                        logger.debug(f"删除图片失败: {e}")
                try:
                    self.img_dir.rmdir()
                except OSError:
                    pass
        except Exception as e:
            logger.error(f"清理 LaTeX 图片时出错: {e}")
