"""
LaTeX 公式渲染模块
支持整段文本渲染为图片（适合长公式和带人设的回答）
"""
import os
import re
import io
import hashlib
import textwrap
from pathlib import Path
from typing import List, Dict, Any, Tuple

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
from matplotlib.patches import Rectangle

from matplotlib.font_manager import FontProperties, fontManager

from astrbot.api import logger
from astrbot.api.message_components import Plain, Image
from astrbot.api.event import AstrMessageEvent


class LatexRenderer:
    """LaTeX 公式渲染器"""

    def __init__(self, img_dir: Path):
        self.img_dir = img_dir
        self.img_dir.mkdir(parents=True, exist_ok=True)
        self.font_properties = self._get_chinese_font()
        
        # 设置全局字体配置
        plt.rcParams['font.family'] = ['sans-serif']
        plt.rcParams['font.sans-serif'] = ['WenQuanYi Micro Hei', 'Noto Sans CJK SC', 
                                           'SimHei', 'Microsoft YaHei', 'DejaVu Sans']
        plt.rcParams['axes.unicode_minus'] = False

    def _get_chinese_font(self) -> FontProperties:
        """获取中文字体 - 优先使用文件路径"""
        # 常见Linux中文字体路径
        font_paths = [
            '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
            '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
            '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
            '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
        ]
        
        for path in font_paths:
            if os.path.exists(path):
                try:
                    fp = FontProperties(fname=path, size=12)
                    logger.info(f"[LatexRenderer] 使用字体文件: {path}")
                    return fp
                except Exception as e:
                    logger.debug(f"[LatexRenderer] 字体加载失败 {path}: {e}")
        
        # 回退：通过名称
        font_names = ['WenQuanYi Micro Hei', 'Noto Sans CJK SC', 
                     'SimHei', 'DejaVu Sans']
        available = [f.name for f in fontManager.ttflist]
        
        for name in font_names:
            if name in available:
                logger.info(f"[LatexRenderer] 使用系统字体: {name}")
                return FontProperties(family=name, size=12)
        
        logger.warning("[LatexRenderer] 未找到中文字体，使用默认")
        return FontProperties(size=12)

    def render_latex(self, formula: str, display_mode: bool = False) -> bytes:
        """渲染单个 LaTeX 公式为图片"""
        try:
            formula = formula.strip()

            plt.figure(figsize=(0.01, 0.01))
            if display_mode:
                tex = f"$${formula}$$"
            else:
                tex = f"${formula}$"

            plt.text(0.5, 0.5, tex, fontsize=14, ha='center', va='center')
            plt.axis('off')
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=150, bbox_inches='tight', pad_inches=0.3)
            plt.close()
            buf.seek(0)
            return buf.read()
        except Exception as e:
            logger.error(f"LaTeX 渲染失败: {e}")
            plt.figure(figsize=(4, 1))
            plt.text(0.5, 0.5, "[公式渲染失败]", fontsize=12, ha='center', va='center')
            plt.axis('off')
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
            plt.close()
            buf.seek(0)
            return buf.read()

    def render_whole_text(self, text: str, title: str = "") -> bytes:
        """
        将整个文本渲染为一张长图片
        适合渲染包含 LaTeX 公式的完整回答

        Args:
            text: 要渲染的文本（支持Markdown和LaTeX混合）
            title: 可选标题

        Returns:
            图片二进制数据
        """
        try:
            # 处理文本：保留换行，但限制每行长度
            lines = text.split('\n')
            processed_lines = []

            for line in lines:
                if line.strip().startswith('$$') and line.strip().endswith('$$'):
                    # 独立公式行，不截断
                    processed_lines.append(line.strip())
                elif line.strip().startswith('$') and line.strip().endswith('$'):
                    # 行内公式
                    processed_lines.append(line.strip())
                else:
                    # 普通文本，自动换行
                    if len(line) > 80:
                        wrapped = textwrap.fill(line, width=80, break_long_words=False, break_on_hyphens=False)
                        processed_lines.extend(wrapped.split('\n'))
                    else:
                        processed_lines.append(line)

            # 计算图片尺寸
            line_height = 0.4
            padding = 0.8
            title_height = 0.6 if title else 0

            total_height = len(processed_lines) * line_height + padding * 2 + title_height
            width = 10  # 固定宽度

            # 限制最大高度（防止图片过长）
            max_height = 30
            if total_height > max_height:
                total_height = max_height
                logger.warning(f"文本过长，已截断渲染（原{len(processed_lines)}行）")

            fig, ax = plt.subplots(figsize=(width, total_height))
            ax.set_xlim(0, width)
            ax.set_ylim(0, total_height)
            ax.axis('off')

            # 背景色（淡蓝色，看起来像对话框）
            fig.patch.set_facecolor('#f0f8ff')
            ax.set_facecolor('#f0f8ff')

            # 渲染标题
            current_y = total_height - padding - title_height/2
            if title:
                ax.text(width/2, current_y, title, 
                       fontsize=16, weight='bold', 
                       ha='center', va='center',
                       fontproperties=self.font_properties)
                current_y -= title_height

            # 渲染内容
            x_start = 0.5
            for i, line in enumerate(processed_lines):
                if current_y < padding:
                    break  # 超出底部，停止渲染

                # 处理行内LaTeX公式的高亮
                if line.strip().startswith('$$') and line.strip().endswith('$$'):
                    # 行间公式，居中，字体稍大
                    formula = line.strip()[2:-2].strip()
                    ax.text(width/2, current_y, f"$${formula}$$",
                           fontsize=13, ha='center', va='top',
                           bbox=dict(boxstyle='round', facecolor='#e6f3ff', alpha=0.8))
                elif '$' in line:
                    # 包含行内公式的文本
                    ax.text(x_start, current_y, line,
                           fontsize=12, ha='left', va='top',
                           fontproperties=self.font_properties)
                else:
                    # 普通文本
                    ax.text(x_start, current_y, line,
                           fontsize=12, ha='left', va='top',
                           fontproperties=self.font_properties)

                current_y -= line_height

            # 添加装饰性边框
            rect = plt.Rectangle((0.2, 0.2), width-0.4, total_height-0.4, 
                                fill=False, edgecolor='#b0c4de', linewidth=2, alpha=0.5)
            ax.add_patch(rect)

            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=150, bbox_inches='tight', 
                       facecolor='#f0f8ff', edgecolor='none', pad_inches=0.3)
            plt.close()
            buf.seek(0)
            return buf.read()

        except Exception as e:
            logger.error(f"整段文本渲染失败: {e}")
            # 失败时返回简单错误图片
            plt.figure(figsize=(6, 2))
            plt.text(0.5, 0.5, f"[文本渲染失败]\n{str(e)[:50]}", 
                    fontsize=12, ha='center', va='center')
            plt.axis('off')
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
            plt.close()
            buf.seek(0)
            return buf.read()

    def process_latex(self, text: str) -> List[Dict[str, Any]]:
        """处理文本中的 LaTeX 公式（分段渲染模式）"""
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

    async def send_with_latex(self, event: AstrMessageEvent, text: str, render_whole: bool = False):
        """
        发送带 LaTeX 公式的消息

        Args:
            event: 消息事件
            text: 文本内容
            render_whole: 是否将整个文本渲染为一张图片（适合长公式和带人设回答）
        """
        try:
            if render_whole:
                # 整段渲染为图片
                img_data = self.render_whole_text(text)
                img_hash = hashlib.md5(img_data).hexdigest()[:12]
                img_path = self.img_dir / f"whole_{img_hash}.png"

                if not img_path.exists():
                    with open(img_path, 'wb') as f:
                        f.write(img_data)

                await event.send(event.chain_result([Image.fromFileSystem(str(img_path))]))
            else:
                # 分段渲染模式（原有逻辑）
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