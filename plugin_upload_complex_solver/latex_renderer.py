"""
LaTeX 公式渲染模块
"""
import os
import re
import io
import hashlib
import textwrap
from pathlib import Path
from typing import List, Dict, Any

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
from matplotlib.patches import FancyBboxPatch, Rectangle
from matplotlib.colors import LinearSegmentedColormap
import numpy as np

from astrbot.api import logger
from astrbot.api.message_components import Plain, Image
from astrbot.api.event import AstrMessageEvent


class LatexRenderer:
    """现代化 LaTeX 公式渲染器（支持中文和自动换行）"""

    def __init__(self, img_dir: Path):
        self.img_dir = img_dir
        self.img_dir.mkdir(parents=True, exist_ok=True)
        
        # 尝试获取中文字体
        self.font_properties = self._get_chinese_font()
        
        # 现代化的颜色主题（浅色模式 - 类似Notion风格）
        self.theme = {
            'bg_color': '#FAFAFA',           # 柔和的米白背景
            'card_bg': '#FFFFFF',            # 卡片白色
            'text_color': '#2C3E50',         # 深蓝灰色文字
            'accent_color': '#3498DB',       # 强调蓝色
            'secondary_color': '#7F8C8D',    # 次要文字灰色
            'border_color': '#E0E0E0',       # 边框浅灰
            'code_bg': '#F5F7FA',            # 代码块背景
            'formula_bg': '#F8F9FA',         # 公式背景
            'shadow_color': '#00000020',     # 阴影颜色
        }
        
        # 设置全局字体 - 添加中文字体支持
        plt.rcParams['font.family'] = ['DejaVu Sans']
        plt.rcParams['axes.unicode_minus'] = False
        plt.rcParams['mathtext.fontset'] = 'stixsans'  # 使用更现代的数学字体
        
        # 如果获取到了中文字体，配置 mathtext 使用自定义字体以支持中文
        if self.font_properties:
            try:
                # 尝试设置 mathtext 使用普通文本字体渲染 \text{}
                plt.rcParams['mathtext.fontset'] = 'custom'
                plt.rcParams['mathtext.it'] = self.font_properties.get_name() + ':italic'
                plt.rcParams['mathtext.rm'] = self.font_properties.get_name()
                plt.rcParams['mathtext.tt'] = self.font_properties.get_name()
                plt.rcParams['mathtext.cal'] = self.font_properties.get_name()
                plt.rcParams['mathtext.bf'] = self.font_properties.get_name() + ':bold'
            except Exception as e:
                logger.debug(f"配置 mathtext 字体失败: {e}")

    def _get_chinese_font(self) -> FontProperties:
        """获取中文字体 - 优先使用系统字体"""
        font_paths = [
            # macOS
            '/System/Library/Fonts/PingFang.ttc',
            '/System/Library/Fonts/STHeiti Light.ttc',
            '/System/Library/Fonts/Hiragino Sans GB.ttc',
            # Linux
            '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
            '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
            '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
            '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
            # Windows
            'C:/Windows/Fonts/simhei.ttf',
            'C:/Windows/Fonts/simsun.ttc',
            'C:/Windows/Fonts/msyh.ttc',
        ]
        
        for path in font_paths:
            if os.path.exists(path):
                try:
                    return FontProperties(fname=path, size=12)
                except:
                    continue
        
        # 回退到系统字体名称
        try:
            from matplotlib import font_manager
            available = [f.name for f in font_manager.fontManager.ttflist]
            for name in ['PingFang SC', 'Heiti SC', 'Noto Sans CJK SC', 'WenQuanYi Micro Hei', 'SimHei', 'Microsoft YaHei']:
                if name in available:
                    return FontProperties(family=name, size=12)
        except:
            pass
        
        return FontProperties(size=12)

    def _create_gradient_background(self, fig, ax, color1='#FAFAFA', color2='#F0F4F8'):
        """创建渐变背景"""
        gradient = np.linspace(0, 1, 256).reshape(256, -1)
        gradient = np.vstack((gradient, gradient))
        
        ax.imshow(gradient, extent=[0, 1, 0, 1], aspect='auto', 
                 cmap=LinearSegmentedColormap.from_list('', [color1, color2]),
                 alpha=0.3, zorder=0)
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)

    def _contains_chinese(self, text: str) -> bool:
        """检查文本是否包含中文字符"""
        for char in text:
            if '\u4e00' <= char <= '\u9fff':
                return True
        return False

    def render_latex(self, formula: str, display_mode: bool = False) -> bytes:
        """渲染单个 LaTeX 公式 - 现代化样式（支持中文）"""
        try:
            formula = formula.strip()
            
            # 创建图形
            fig = plt.figure(figsize=(8, 2))
            ax = fig.add_subplot(111)
            
            # 设置公式背景
            if display_mode:
                # 如果包含中文，使用文本模式包裹
                if self._contains_chinese(formula):
                    tex = f"$\\text{{{formula}}}$"
                else:
                    tex = f"$${formula}$$"
                fontsize = 16
                padding = 0.4
            else:
                # 行内公式
                if self._contains_chinese(formula):
                    tex = f"$\\text{{{formula}}}$"
                else:
                    tex = f"${formula}$"
                fontsize = 14
                padding = 0.3
            
            # 渲染文字 - 添加中文字体支持
            text = ax.text(0.5, 0.5, tex, fontsize=fontsize, 
                          ha='center', va='center',
                          color=self.theme['text_color'],
                          transform=ax.transAxes,
                          fontproperties=self.font_properties)  # 修复：添加中文字体
            
            # 关闭坐标轴
            ax.axis('off')
            
            # 调整布局
            plt.tight_layout(pad=padding)
            
            # 保存
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=200, bbox_inches='tight',
                       facecolor=self.theme['formula_bg'], edgecolor='none')
            plt.close(fig)
            buf.seek(0)
            return buf.read()
            
        except Exception as e:
            logger.error(f"LaTeX渲染失败: {e}")
            # 返回错误提示图（也使用中文字体）
            fig, ax = plt.subplots(figsize=(6, 1.5))
            ax.text(0.5, 0.5, "[公式渲染失败]", fontsize=12, 
                   ha='center', va='center', color='#E74C3C',
                   transform=ax.transAxes,
                   fontproperties=self.font_properties)  # 修复：添加中文字体
            ax.axis('off')
            ax.set_facecolor(self.theme['bg_color'])
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
            plt.close()
            buf.seek(0)
            return buf.read()

    def render_whole_text(self, text: str, title: str = "") -> bytes:
        """
        整段文本渲染 - 现代化卡片设计（支持中文和自动换行）
        """
        try:
            # 预处理文本 - 改进的自动换行
            lines = self._preprocess_text(text)
            
            # 计算布局参数
            width = 12  # 固定宽度（英寸）
            line_height = 0.5
            padding = 1.0
            header_height = 1.2 if title else 0
            
            # 计算总高度（考虑行数）
            content_height = len(lines) * line_height
            total_height = content_height + padding * 2 + header_height
            
            # 限制最大高度
            max_height = 40
            if total_height > max_height:
                total_height = max_height
                lines = lines[:int((max_height - padding*2 - header_height) / line_height)]
                lines.append("...(内容已截断)")
            
            # 创建图形
            fig = plt.figure(figsize=(width, total_height))
            ax = fig.add_subplot(111)
            
            # 绘制卡片背景（带圆角和阴影效果）
            card = FancyBboxPatch((0.05, 0.02), 0.9, 0.96,
                                 boxstyle="round,pad=0.02,rounding_size=0.02",
                                 facecolor=self.theme['card_bg'],
                                 edgecolor=self.theme['border_color'],
                                 linewidth=1.5,
                                 transform=ax.transAxes,
                                 zorder=1)
            ax.add_patch(card)
            
            # 如果有标题，绘制标题栏
            current_y = 0.95
            if title:
                title_bg = Rectangle((0.05, 0.88), 0.9, 0.1,
                                    facecolor=self.theme['accent_color'],
                                    alpha=0.1,
                                    transform=ax.transAxes,
                                    zorder=2)
                ax.add_patch(title_bg)
                
                ax.text(0.5, 0.93, title, fontsize=16, weight='bold',
                       ha='center', va='center',
                       color=self.theme['accent_color'],
                       transform=ax.transAxes,
                       zorder=3,
                       fontproperties=self.font_properties)
                
                current_y = 0.85
            
            # 渲染内容行
            x_start = 0.08
            y_step = line_height / total_height
            
            for i, line in enumerate(lines):
                if current_y < 0.05:
                    break
                
                y_pos = current_y
                
                # 处理不同类型的行
                if line.startswith('$$') and line.endswith('$$'):
                    # 行间公式
                    formula = line[2:-2].strip()
                    # 如果公式包含中文，使用\text包裹
                    if self._contains_chinese(formula):
                        display_tex = f"$\\text{{{formula}}}$"
                    else:
                        display_tex = f"$${formula}$$"
                    
                    ax.text(0.5, y_pos, display_tex,
                           fontsize=14, ha='center', va='top',
                           color=self.theme['text_color'],
                           transform=ax.transAxes,
                           fontproperties=self.font_properties)
                    
                elif line.startswith('```') or line.startswith('`'):
                    # 代码块
                    code_text = line.strip('`')
                    ax.text(x_start, y_pos, code_text,
                           fontsize=11, ha='left', va='top',
                           color=self.theme['text_color'],
                           family='monospace',
                           transform=ax.transAxes,
                           fontproperties=self.font_properties,
                           bbox=dict(boxstyle='round,pad=0.3',
                                    facecolor=self.theme['code_bg'],
                                    edgecolor='none',
                                    alpha=0.8))
                    
                elif line.startswith('#'):
                    # 标题
                    level = len(line) - len(line.lstrip('#'))
                    title_text = line.lstrip('#').strip()
                    font_size = max(14, 18 - level * 2)
                    weight = 'bold' if level <= 2 else 'normal'
                    
                    ax.text(x_start, y_pos, title_text,
                           fontsize=font_size, weight=weight,
                           ha='left', va='top',
                           color=self.theme['accent_color'] if level == 1 else self.theme['text_color'],
                           transform=ax.transAxes,
                           fontproperties=self.font_properties)
                    
                elif line.startswith('- ') or line.startswith('* '):
                    # 列表项
                    item_text = '• ' + line[2:]
                    ax.text(x_start, y_pos, item_text,
                           fontsize=12, ha='left', va='top',
                           color=self.theme['text_color'],
                           transform=ax.transAxes,
                           fontproperties=self.font_properties)
                    
                elif '$' in line:
                    # 混合文本和公式 - 需要处理包含中文的公式
                    processed_line = line
                    if self._contains_chinese(line):
                        # 简单处理：将 $...$ 中的中文用 \text{} 包裹
                        import re
                        def wrap_chinese_in_text(match):
                            content = match.group(1)
                            if self._contains_chinese(content):
                                return f"$\\text{{{content}}}$"
                            return match.group(0)
                        processed_line = re.sub(r'\$(.*?)\$', wrap_chinese_in_text, line)
                    
                    ax.text(x_start, y_pos, processed_line,
                           fontsize=12, ha='left', va='top',
                           color=self.theme['text_color'],
                           transform=ax.transAxes,
                           fontproperties=self.font_properties)
                else:
                    # 普通文本 - 改进换行处理
                    color = self.theme['text_color']
                    # 处理引用（>开头）
                    if line.startswith('>'):
                        color = self.theme['secondary_color']
                        line = line[1:].strip()
                    
                    # 对长文本进行自动换行处理
                    if len(line) > 80:
                        wrapped_lines = textwrap.wrap(line, width=80, 
                                                     break_long_words=False,
                                                     break_on_hyphens=False)
                        for j, wrapped_line in enumerate(wrapped_lines):
                            if current_y < 0.05:
                                break
                            ax.text(x_start, y_pos - j * y_step * 0.8, wrapped_line,
                                   fontsize=12, ha='left', va='top',
                                   color=color,
                                   transform=ax.transAxes,
                                   wrap=True,
                                   fontproperties=self.font_properties)
                        # 调整current_y以补偿多行
                        current_y -= (len(wrapped_lines) - 1) * y_step * 0.8
                    else:
                        ax.text(x_start, y_pos, line,
                               fontsize=12, ha='left', va='top',
                               color=color,
                               transform=ax.transAxes,
                               wrap=True,
                               fontproperties=self.font_properties)
                
                current_y -= y_step * 0.8
            
            # 设置坐标轴
            ax.set_xlim(0, 1)
            ax.set_ylim(0, 1)
            ax.axis('off')
            
            # 保存
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=200, bbox_inches='tight',
                       facecolor=self.theme['bg_color'], edgecolor='none',
                       pad_inches=0.5)
            plt.close(fig)
            buf.seek(0)
            return buf.read()
            
        except Exception as e:
            logger.error(f"整段渲染失败: {e}")
            # 返回错误图（使用中文字体）
            fig, ax = plt.subplots(figsize=(6, 2))
            ax.text(0.5, 0.5, f"[渲染失败]\n{str(e)[:50]}", 
                   fontsize=12, ha='center', va='center', color='#E74C3C',
                   fontproperties=self.font_properties)
            ax.axis('off')
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
            plt.close()
            buf.seek(0)
            return buf.read()

    def _preprocess_text(self, text: str) -> List[str]:
        """预处理文本，优化排版和自动换行"""
        lines = text.split('\n')
        processed = []
        
        for line in lines:
            line = line.rstrip()
            if not line:
                processed.append('')  # 保留空行作为间距
                continue
            
            # 检查是否是长行（超过80字符）且不是代码块或公式
            is_code = line.startswith('```') or line.startswith('`')
            is_formula = line.startswith('$$') or (line.startswith('$') and line.endswith('$'))
            is_header = line.startswith('#')
            
            if len(line) > 80 and not is_code and not is_formula and not is_header:
                # 对长行进行智能换行
                # 优先在标点符号处换行
                wrapped = self._smart_wrap(line, width=80)
                processed.extend(wrapped)
            else:
                processed.append(line)
        
        return processed

    def _smart_wrap(self, text: str, width: int = 80) -> List[str]:
        """
        智能换行：优先在标点符号后换行，保持中文句子完整
        """
        import re
        
        if len(text) <= width:
            return [text]
        
        lines = []
        current_line = ""
        
        # 按句子分割（中英文标点）
        sentences = re.split(r'([。！？\.!\?；;，,])', text)
        
        for i in range(0, len(sentences), 2):
            sentence = sentences[i]
            punctuation = sentences[i+1] if i+1 < len(sentences) else ""
            segment = sentence + punctuation
            
            # 如果当前行加上新段落后超过宽度，且当前行不为空，则换行
            if len(current_line) + len(segment) > width and current_line:
                lines.append(current_line)
                current_line = segment
            else:
                current_line += segment
            
            # 如果单个段落就超过宽度，强制按字符截断
            while len(current_line) > width:
                lines.append(current_line[:width])
                current_line = current_line[width:]
        
        if current_line:
            lines.append(current_line)
        
        return lines if lines else [text]

    def process_latex(self, text: str) -> List[Dict[str, Any]]:
        """分段处理文本中的LaTeX公式"""
        # 匹配行间公式 $$...$$ 和行内公式 $...$
        pattern = r'(\$\$.*?\$\$|\$.*?\$)'
        parts = re.split(pattern, text, flags=re.DOTALL)
        result = []
        
        for idx, part in enumerate(parts):
            if not part:
                continue
            
            if part.startswith('$$') and part.endswith('$$'):
                # 行间公式
                formula = part[2:-2].strip()
                try:
                    img_data = self.render_latex(formula, display_mode=True)
                    result.append({'type': 'image', 'data': img_data, 'index': idx})
                except Exception as e:
                    logger.error(f"渲染行间公式失败: {e}")
                    result.append({'type': 'text', 'text': part, 'index': idx})
                    
            elif part.startswith('$') and part.endswith('$'):
                # 行内公式
                formula = part[1:-1].strip()
                try:
                    img_data = self.render_latex(formula, display_mode=False)
                    result.append({'type': 'image', 'data': img_data, 'index': idx})
                except Exception as e:
                    logger.error(f"渲染行内公式失败: {e}")
                    result.append({'type': 'text', 'text': part, 'index': idx})
            else:
                # 普通文本
                if part.strip():
                    result.append({'type': 'text', 'text': part, 'index': idx})
        
        result.sort(key=lambda x: x['index'])
        return result

    async def send_with_latex(self, event: AstrMessageEvent, text: str, render_whole: bool = False):
        """发送带LaTeX的消息"""
        try:
            if render_whole:
                # 整段渲染为一张图
                img_data = self.render_whole_text(text)
                img_hash = hashlib.md5(img_data).hexdigest()[:12]
                img_path = self.img_dir / f"render_{img_hash}.png"
                
                if not img_path.exists():
                    with open(img_path, 'wb') as f:
                        f.write(img_data)
                
                await event.send(event.chain_result([Image.fromFileSystem(str(img_path))]))
            else:
                # 分段渲染
                segments = self.process_latex(text)
                chain = []
                
                for seg in segments:
                    if seg['type'] == 'text':
                        if seg['text'].strip():
                            chain.append(Plain(seg['text']))
                    else:
                        # 保存公式图片
                        img_hash = hashlib.md5(seg['data']).hexdigest()[:12]
                        img_path = self.img_dir / f"latex_{img_hash}.png"
                        
                        if not img_path.exists():
                            with open(img_path, 'wb') as f:
                                f.write(seg['data'])
                        chain.append(Image.fromFileSystem(str(img_path)))
                
                if chain:
                    await event.send(event.chain_result(chain))
                else:
                    # 如果没有内容，发送原文本
                    await event.send(event.plain_result(text))
                    
        except Exception as e:
            logger.error(f"发送LaTeX消息失败: {e}")
            # 失败时发送原文
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
            logger.error(f"清理LaTeX图片时出错: {e}")