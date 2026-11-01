# WX-005 结果：SSE 背压、停止流程与重连管理

## 实现（sse_consumer.py 重写）

1. **有界线程→事件循环通道**：读取线程写有界 `queue.Queue(maxsize=8)`
   （阻塞 put，天然限流），事件循环 pump 任务搬运到有界 asyncio.Queue
   （512）。总积压上限 = queue_size + thread_channel_size，与回调速度无关。
   旧实现 1100 条输入产生 588 个未完成 `run_coroutine_threadsafe(put)` 的
   形态已消除（WX-000 断言 pending ≤ 2 通过）。
2. **可中断停止**：stop() 设事件 → 关闭响应对象（解锁阻塞读）→ 取消
   `_run` 任务（带 3s 超时 shield 等待）→ 清空线程通道 → 关闭专用线程池。
   读取线程用**专用 ThreadPoolExecutor**（`shutdown(wait=False,
   cancel_futures=True)`），不再占用默认 executor；feeder 用带超时的
   `channel.get(True, 0.5)` 轮询，无永久阻塞线程。无消息时也能停止。
3. **统一重连**：仅 `_run()` 内部做指数退避（2s×1.5 封顶 15s）；
   正常 EOF / 异常 / 人工停止分别处理；看门狗只在消费者实例丢失或
   `gave_up and not running` 时重建，不再反复取消连接中的任务。
   `on_reconnected` 回调（线程安全调度回事件循环）供 WX-006 补收挂钩。
4. **解析健壮**：心跳/空行跳过；坏 JSON 丢弃不杀消费链；异常数据测试通过。

## 测试

- WX-000：1100 条突发 + 慢回调，pending ≤ 2
- Wx005SseTests：无消息停止干净退出；坏数据不杀链（收到 [1,2] 两条合法）；
  慢消费下积压有界且释放后全部消费（50 条不丢）
- 宿主进程退出验证：stop 后线程均为可回收状态（探针 os._exit(0) 验证）

结果：全绿。
