# AstrBot morechatplus Chat Plugin

QQ群聊增强插件，提供智能上下文管理、用户画像维护、主动回复判定、图片识别和艾特功能。

## 功能特性

### 1. 上下文管理
- 数据库存储所有群聊消息
- 自动清理过期消息（可配置保留天数）
- 支持引用消息追踪
- 上下文格式：`[昵称|user_id|时间]:(消息ID) <引用信息> 内容`

### 2. 用户画像
- 与QQ号强绑定的用户画像
- 自动识别用户昵称变化
- 每天早上6点自动更新用户画像
- 支持用户身份验证（防止冒充）

### 3. 主动回复
- 模型A每10条消息总结一次上下文
- 分析话题走向和与bot的关联度
- 严格判定是否需要主动回复
- 避免参与有争议的话题

### 4. 图片识别
- 支持@bot或包含bot名字时自动识图
- 可配置识图模型和提示词
- 识图结果自动加入上下文

### 5. 艾特功能
- 支持 `[at:QQ号]` 格式艾特
- 自动解析并转换为平台At组件
- 支持昵称查询和映射

## 配置说明

### 核心设置
- `enable`: 启用/禁用插件
- `trigger_words`: 触发回复的关键词（逗号分隔）
- `admin_user_id`: 管理员QQ号（消息会特殊标注）
- `bot_name`: Bot名字
- `bot_qq_id`: Bot的QQ号

### 模型配置
- `main_llm_provider`: 主回复模型
- `model_a_provider`: 上下文总结模型
- `model_b_provider`: 用户画像分析模型
- `vision_provider`: 图片识别模型
- `vision_prompt`: 识图提示词

### 上下文配置
- `max_context_messages`: 主LLM上下文条数（默认100）
- `model_a_context_messages`: 模型A上下文条数（默认150）
- `summary_interval`: 总结间隔（默认10条消息）
- `context_max_age_days`: 上下文保留天数（默认7天）

### 主动回复配置
- `enable`: 启用主动回复
- `trigger_keyword`: 主动回复触发标记（默认`[ACTIVE_REPLY]`）
- `strict_mode`: 严格模式
- `avoid_controversial`: 避免争议话题

### 用户画像配置
- `enable`: 启用用户画像
- `daily_update_hour`: 每日更新时间（默认6点）
- `max_daily_messages`: 每日最大分析消息数（默认500）
- `nickname_check_groups`: 昵称检查消息组数（默认20）

## 消息格式

### 上下文格式示例
```
[猫猫|13286633|19:20:05]:(#msg267518526) <引用信息: #msg267518526> [at:小死神] 可爱喵~
[蓝兔|42864594|19:21:12]:(#msg267518527) 确实可爱~
```

### 支持的标签
- `[at:QQ号]` - @某人
- `[image:ID]` - 图片标记
- `<引用:消息ID>` - 引用消息

## LLM工具

插件提供以下LLM工具供模型调用：

1. `morechatplus_get_message(message_id)` - 获取指定消息内容
2. `morechatplus_get_user_profile(user_id)` - 获取用户画像
3. `morechatplus_query_nickname(nickname)` - 查询昵称对应的用户
4. `morechatplus_get_context(count)` - 获取最近上下文
5. `morechatplus_add_nickname(user_id, nickname)` - 添加用户昵称

## 安装

1. 将插件目录放到 `data/plugins/`
2. 重启 AstrBot
3. 在插件配置页面进行配置

## 推荐设置

为避免能力重叠，建议：
- 关闭内置群聊上下文：`group_icl_enable`
- 关闭内置主动回复：`active_reply.enable`
- 关闭内置引用回复：`reply_with_quote`

## 数据存储

插件数据目录：
```
data/plugin_data/morechatplus/
└── chat_data.db
```

数据库包含以下表：
- `messages`: 消息记录
- `context_summaries`: 上下文总结
- `user_profiles`: 用户画像
- `nickname_mappings`: 昵称映射


## 新增功能：模型故障转移（Fallback）

从 v1.1.0 开始，模型A（上下文总结）和模型B（用户画像）支持配置备用模型。当主模型调用失败（如额度不足、服务超时、API错误等）时，系统会自动切换到备用模型。

### 配置方法

在插件配置的「模型配置」部分：

- **模型A备用提供商ID**: 当模型A主模型失败时使用的备用模型
- **模型B备用提供商ID**: 当模型B主模型失败时使用的备用模型

### 故障转移行为

1. **自动检测**: 系统会检测以下失败情况：
   - 模型调用超时
   - API额度不足（返回错误）
   - 服务不可用
   - 其他异常错误

2. **自动切换**: 主模型失败后，会立即尝试备用模型（如果配置了）

3. **日志记录**: 日志中会标记是否使用了备用模型，例如：
   ```
   [MoreChatPlus] 模型A已切换到备用模型: openai_backup
   [MoreChatPlus] 更新用户画像: 123456 @ group_xxx (via openai_backup) [备用]
   ```

4. **上报追踪**: 所有模型调用（包括备用模型）都会上报到 LLM Debugger（如果已安装），包含 `is_fallback` 标记

### 使用建议

- **成本优化**: 可以将便宜的模型（如硅基流动、DeepSeek）作为主模型，将GPT-4等高性能模型作为备用模型
- **稳定性**: 建议配置备用模型以确保关键功能（如用户画像分析）的可靠性
- **留空禁用**: 如果不希望使用备用模型，将备用提供商ID留空即可


## 更新日志

### v1.1.0
- 新增模型A和模型B的备用模型支持（故障转移）
- 新增 model_utils.py 模块处理模型调用
- 优化错误日志记录

### v1.0.0
- 初始版本发布
- 实现上下文管理、用户画像、主动回复、图片识别、艾特功能


## 更新日志

### v1.2.0
- **新增图片缓存功能**：基于URL/MD5的图片唯一标识，支持使用计数和LRU清理策略
- **新增识图结果缓存**：相同图片不会重复调用识图API
- **新增 [at:QQ号] 标签自动转换**：最终输出时会自动转换为平台At组件
- **修改消息格式**：引用信息现在只显示ID，不包含内容（可通过工具获取）
- **新增图片识图工具**：`morechatplus_get_image_vision(image_id)` 供LLM调用
- **新增图片缓存统计工具**：`morechatplus_get_image_cache_stats()`
- **新增图片缓存配置**：可配置最大缓存数量和是否启用识图缓存

### v1.1.0
- 新增模型A和模型B的备用模型支持（故障转移）
- 新增 model_utils.py 模块处理模型调用
- 优化错误日志记录

### v1.0.0
- 初始版本发布
- 实现上下文管理、用户画像、主动回复、图片识别、艾特功能
