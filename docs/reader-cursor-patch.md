# reader 游标补丁（R4，单独交付）

## 背景

复核 R4：`/api/v1/messages` HTTP 端点不接受 `from_time`（内部 Python
方法支持 ≠ HTTP 支持）；补收按「本页最大时间」分页对倒序返回无效，
600 条缺口只收到 500 条且不报缺口。

## 补丁内容（向后兼容）

隔离副本 `patches/reader-api.cursor-patch.py`（SHA-256
`8e947e531f081d01dd7c03270f87f55dbdce9f67691e824fec7eb68043fcd35d`）相对
生产 `reader-api.py` 新增：

1. `Reader.messages_after(talker, limit, after_time, after_id)`：
   升序分页，严格晚于游标 (after_time, after_id)；`after_id` 是 rawid
   （sha256([talker, serverId, localId, createTime])），同秒多条由其
   内嵌 localId 消歧。返回 `{"messages": [...], "cursor": {...}|None}`。
2. `Reader.row_rawid(talker, row)`：与 push_payload 相同的 rawid 算法。
3. HTTP 路由：`GET /api/v1/messages` 带 `after_time` 或 `after_id`
   参数时走游标契约；**不带这些参数的旧调用行为完全不变**（旧客户端
   兼容性不受影响）。

## 部署依赖

- 插件 1.2.0-wxfix.2 的断线补收优先走游标契约；reader 未打补丁时
  `fetch_messages_page` 请求会得到 404/无 cursor 字段 → 补收退化为
  「单页最新 500 条 + gap 显式记录」（不会静默漏消息，但覆盖不完整）。
- **完整补收验收（>500 条、同秒边界）需要先部署本补丁再重启 reader**。
- 补丁只改 reader-api.py 一个文件；部署 = 备份原文件 → 替换 → 重启
  reader 进程。回退 = 恢复备份。

## 语音接收（R13 附录）

接收方向语音字节解码（微信 4.x silk 音频文件定位 + 解密）在 reader
组件中不存在，属于新功能开发而非缺陷修复。当前插件投递 voice 段
（含描述、空 binary），核心按文本降级渲染；完整「语音转写进入处理
链」需要 reader 侧新增语音文件接口 —— 列为未完成项，不在本轮验收
范围（与「历史进入模型上下文」同等待遇，明确未完成）。

## 语音接收接口规格（未实现，需用户确认范围）

微信 4.x 接收语音的音频文件定位与解密（sqlcipher/文件系统双路径）是
新功能开发，本轮不做假实现。规格预留：

    GET /api/v1/voice/<rawid>
    -> {"success": true, "b64": "<silk bytes>", "duration_ms": int}

实现前置条件：
1. 定位音频载体（cache VoiceTemp / msg attach / message db BLOB 之一，
   需在真机验证，本轮探查受 sqlcipher 访问限制未完成）。
2. SILK→PCM 解码（pilk 已可用）→ 核心语音段 binary_data_base64 填充
   → 宿主 ASR 转写进入处理链。
3. reader 补丁 + 插件 wx_message 语音分支同步更新。

在此之前，语音接收验收项（真实语音内容理解）保持**未完成**状态。