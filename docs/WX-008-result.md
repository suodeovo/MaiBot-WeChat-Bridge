# WX-008 结果：按请求转语音的状态隔离

## 实现（plugin.py）

- `note_inbound_text(session_id, text)`：按**会话键**记录最近入站正文与
  时间（`_voice_requests` 字典，最多 64 会话，LRU 淘汰）—— 替换全插件
  单份 `_last_user_text/_last_user_at`。inbound.submit 在统一身份规范化后
  以 `session_id` 记录。
- `_tts_plan(text, session)`：on_request 判定读**该会话**的请求状态；
  `session=''` 时兼容旧行为（无请求）。出站路径传入解析后的 wxid。
- 消费即失效：请求命中（或否定/过期）后删除该会话条目 —— 连续轮次
  不复用旧请求。
- 窗口 `tts_request_window_s`（默认 120s）过期即失效；否定词优先级高于
  请求词（命中即清）。
- B 群新消息写自己的键，不覆盖 A 群请求；A 群请求不能使 B 群转语音。
- 五种模式语义保持：text_only / on_request / both / voice_only /
  voice_if_short；语音失败兜底只在 `ctx.sent['text']==0` 时补发
  （不重复发送已成功部分，逻辑保留）。

## 测试（Wx008TtsTests）

- A 群请求 + B 群普通文本并发：A=(True,False)，B=(False,True)
- 过期请求失效（window=1s 后判定）
- 否定词压制（「别发语音，打字就行」）
- 请求单次消费（第二次回复不再转语音）
- 五模式语义矩阵（含长短文本阈值）

结果：全绿。合成消息测试，未调用真实付费合成接口或微信发送。
