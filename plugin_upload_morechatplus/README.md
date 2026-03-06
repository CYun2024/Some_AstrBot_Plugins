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
[虹猫猫|28196593|19:20:05]:(#msg267518526) <引用信息: #msg267518526> [at:机巧猫] 可爱喵~
[蓝兔|28196594|19:21:12]:(#msg267518527) 确实可爱~
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

## 更新日志

### v1.0.0
- 初始版本发布
- 实现上下文管理、用户画像、主动回复、图片识别、艾特功能
