# OpenList 共享插件（AstrBot）

一个面向 AstrBot 的 [OpenList](https://github.com/OpenListTeam/OpenList)（AList 社区分支）文件共享插件：
群成员上传文件需先经过 **LLM 审核**，审核通过后上传到 OpenList，**文件名自动备注上传者信息**；
查询时直接返回 **分享链接**（不下载文件本体）。

> 灵感与 API 参考：[astrbot_plugin_openlistfile](https://github.com/Foolllll-J/astrbot_plugin_openlistfile)

## 功能特性

- 📤 **审核后上传**：引用（回复）一条文件消息并发送 ` /ol up`，插件先用 LLM 审核文件（文件名 / 大小 / 文本类文件内容预览），通过后才上传
- ✅ **群内 @ 通知**：审核结果会在群里 @ 上传者并告知（成功含分享链接 / 失败含原因）
- 🏷️ **文件名备注上传者**：上传后文件名按模板自动带上上传者昵称（如 `[小明]报告.pdf`），并本地持久化上传记录，查询时同步展示上传者
- 🔗 **查询即分享链接**：`/ol ls`、`/ol search`、`/ol info` 直接返回文件的分享直链，不下载文件
- 🔐 **敏感信息不进群**：OpenList 地址 / 用户名 / 密码全部放在插件设置里（密码为密文输入框），群聊中永不展示
- 👥 **多群权限**：插件设置中可配置允许使用的群号列表（支持多群），留空则所有群可用

## 安装

1. 把 `astrbot_plugin_openlist_share` 整个目录放入 AstrBot 的 `data/plugins/` 目录（或打包成 zip 在管理面板「插件」页上传安装）。
2. 在管理面板 → 插件 → 找到「OpenList 共享」→ 设置：
   - **OpenList 连接**：填写服务器地址、用户名、密码（用户名留空为匿名访问）；若地址是内网，另填公网地址用于生成群内可访问的分享链接
   - **使用权限**：添加允许使用的群号（可多个）；如需私聊使用，开启「允许私聊」
   - **上传设置**：默认上传目录、大小上限、允许扩展名、文件名备注模板
   - **LLM 审核**：开启审核、选择审核模型提供商（在管理面板里选已配置好的模型；留空则使用当前会话的模型）
3. 完成。群内即可使用。

## 使用方法

| 指令 | 说明 |
| :--- | :--- |
| `/ol up [目标目录]`（别名 `/ol 上传`） | **引用（回复）一条文件消息后发送本指令**，文件经 LLM 审核通过后上传；不指定目录则传到「默认上传目录」 |
| `/ol ls [路径]`（别名 `/ol 列表`） | 列出目录内容，每个文件附带分享链接与上传者 |
| `/ol search 关键词`（别名 `/ol 搜索`） | 搜索文件，返回匹配项的分享链接 |
| `/ol info /路径/文件名`（别名 `/ol 信息`） | 查看单个文件详情（大小、修改时间、上传者、分享链接） |
| `/ol help`（别名 `/ol 帮助`） | 显示帮助 |

示例：

```
# 上传：先引用（回复）一条文件消息，再发送
/ol up
/ol up /共享

# 查询
/ol ls /
/ol ls /共享
/ol search 报告
/ol info /共享/[小明]报告.pdf
```

### 上传流程

1. 成员引用一条包含文件的消息，发送 `/ol up [目录]`
2. 插件回复「已收到文件，正在审核……」
3. LLM 审核（检查违禁内容 / 恶意代码 / 不当信息；文本类文件会附带内容预览）
4. 通过 → 上传到 OpenList，文件名按模板备注上传者 → 群里 **@ 上传者** 发送上传成功 + 分享链接
5. 不通过 → 群里 **@ 上传者** 说明拒绝原因，文件不会上传

## 配置项说明

| 配置 | 说明 |
| :--- | :--- |
| `connection.openlist_url` | OpenList 服务器地址，如 `http://127.0.0.1:5244` |
| `connection.public_openlist_url` | 公网可访问地址（可选），用于替换分享链接中的内网地址 |
| `connection.username / password` | 登录凭据（密文展示），留空为匿名访问 |
| `permission.allowed_groups` | 允许使用本插件的群号列表（多群），留空 = 所有群 |
| `permission.allow_private_chat` | 是否允许私聊使用 |
| `upload.default_upload_path` | `/ol up` 未指定目录时的默认上传目录 |
| `upload.max_upload_size_mb` | 单文件大小上限，0 = 不限 |
| `upload.allowed_extensions` | 允许的扩展名（逗号分隔），留空 = 不限 |
| `upload.rename_with_uploader` | 是否在文件名中备注上传者 |
| `upload.rename_template` | 备注模板，占位符 `{uploader}`、`{filename}`，留空不改名 |
| `llm_review.enable` | 是否启用 LLM 审核（关闭后直接上传） |
| `llm_review.provider` | 审核用模型提供商（管理面板下拉选择） |
| `llm_review.system_prompt` | 审核提示词（`<default>` 表示使用内置默认） |
| `llm_review.text_preview_chars` | 文本类文件内容预览长度，0 = 不预览 |
| `display.max_list_items` | 列表单次显示的最大条数 |

## 数据存储

```
data/plugin_data/astrbot_plugin_openlist_share/
└── upload_records.json   # 上传记录（文件路径、上传者、时间等）
```

记录文件不会被群聊读取，仅用于查询时展示「上传者」备注。

## 常见问题

**Q: 上传提示「LLM 审核模型不可用」**
A: 在插件设置 → LLM 审核中通过下拉选择已配置好的模型提供商；或确认 AstrBot 的模型提供商配置正常。若暂时不想审核，可关闭「启用 LLM 审核」。

**Q: 分享链接打不开**
A: 多为内网地址导致。请在设置中填写「公网分享地址」；另外确保 OpenList 对应目录的「全部签名」选项已开启（否则部分链接无签名）。

**Q: 上传成功但群里没收到分享链接**
A: 上传本身成功。分享链接获取失败通常是 OpenList 未开启签名或目录权限不足，可联系管理员开启「全部签名」。

**Q: 想改文件名备注格式**
A: 在 `upload.rename_template` 中修改模板，例如 `{filename}`（不加备注）或 `{filename}[{uploader}]`。

## 许可

本项目基于 [astrbot_plugin_openlistfile](https://github.com/Foolllll-J/astrbot_plugin_openlistfile) 的 API 调用思路进行简化开发，遵循 AGPL-3.0。
