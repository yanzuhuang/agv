# AGV TCP 收报测试

`tcp_receiver.py` 使用 Python 标准库模拟 AGV 的 TCP 接收端，持续监听 1024 端口，将收到的原始字节打印为大写 HEX。无需安装第三方包，适用于 Python 3.8 及以上版本。客户端断开后仍可继续接收新的连接，也支持多个客户端连接。按 Ctrl+C 停止。

`tcp_sender.py` 是配套的发送测试脚本。它把 HEX 字符串转换成真实字节后发送，例如 `AA BB` 发送两个字节，而不是发送字符 `A A 空格 B B`。示例报文仅用于验证收发，不是 AGV 的实际控制指令。

## 一、同一台电脑先测试

在脚本所在目录打开两个终端。第一个终端运行：

```powershell
python tcp_receiver.py --host 127.0.0.1
```

第二个终端运行：

```powershell
python tcp_sender.py --host 127.0.0.1 --hex "01 02 03 04 AA BB CC DD"
```

接收端应打印类似内容（时间和客户端端口会不同）：

```text
[2026-10-08 14:00:00] 来源 127.0.0.1:53001 | 收到 8 字节 | HEX: 01 02 03 04 AA BB CC DD
```

## 二、按老师要求使用两台电脑

1. 电脑 A 用于编写和运行发送脚本。将 `tcp_receiver.py` 复制到电脑 B，电脑 B 用来模拟 AGV。两台电脑均需要 Python；电脑 A 运行配套发送脚本时，将 `tcp_sender.py` 和 `tcp_receiver.py` 放在同一目录。
2. 两台电脑接入相互可访问的同一局域网，为电脑 B 配置静态 IPv4 地址。在电脑 B 运行 `ipconfig` 确认所连接网卡的 IPv4 地址。比如 B 为 `192.168.1.100`、A 为 `192.168.1.101`，两台子网掩码均为 `255.255.255.0`。这是示例；按实际网络使用未被占用的地址。
3. 在电脑 B 的脚本目录启动接收端，并保持终端打开：

   ```powershell
   python tcp_receiver.py
   ```

   默认监听 `0.0.0.0:1024`，即所有本地 IPv4 网卡。也可以指定 B 的网卡地址：

   ```powershell
   python tcp_receiver.py --host 192.168.1.100 --port 1024
   ```

4. 如果 Windows 防火墙阻止连接，在电脑 B 的防火墙设置中允许 Python 接收专用网络连接，或放行 TCP 1024 入站端口。已将该局域网设置为“专用网络”时，可以在管理员 PowerShell 中建立仅限本地子网的规则：

   ```powershell
   New-NetFirewallRule -DisplayName "AGV TCP Test 1024" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 1024 -Profile Private -RemoteAddress LocalSubnet
   ```

5. 在电脑 A 上，发送程序的**目标 IP 填电脑 B 的静态 IP**，目标端口填 **1024**。配套脚本示例：

   ```powershell
   python tcp_sender.py --host 192.168.1.100 --port 1024 --hex "01 02 03 04 AA BB CC DD"
   ```

6. 查看电脑 B 的接收窗口。如果出现来自电脑 A 的连接记录，并打印 `HEX: 01 02 03 04 AA BB CC DD`，说明本次 TCP 收报测试通过。可以多次运行发送脚本，或将 `--hex` 替换成需要验证的实际报文。

**地址区别：** `127.0.0.1` 永远指向运行程序的本机，不能用于两台电脑互连。`0.0.0.0` 用作接收端监听地址；发送端应填写电脑 B 的实际 IPv4 地址。

## 三、排查与测试边界

- **连接被拒绝：** 确认电脑 B 已启动接收脚本，目标 IP 和端口正确。
- **连接超时：** 检查两台电脑的网段、网络连通性、防火墙和 Wi-Fi 客户端隔离。电脑 A 可运行 `Test-NetConnection 192.168.1.100 -Port 1024`，查看 `TcpTestSucceeded`。该命令只测试连接，不发送 HEX 数据，因此接收端可能只打印连接和断开记录。
- **端口被占用：** 在电脑 B 运行 `netstat -ano | findstr :1024`，检查是否已有接收脚本或其他程序监听该端口。避免重复启动接收端。
- **一条报文显示为多行，或多条报文合成一行：** TCP 是字节流，`recv()` 的一次返回不一定对应一条协议报文。本脚本按每次收到的数据块打印 HEX，比较时应按同一连接内的顺序拼接字节。要分辨完整 AGV 报文，需要再按照协议的长度字段、报文头等规则拆包。
- **实际 AGV 客户端等待应答：** 该脚本仅验证字节是否到达，不解析命令、不回 ACK、不模拟状态或动作完成。因此客户端可能因等不到应答而超时；接收端的 HEX 输出仍能证明收到的数据。

现有 `warehouse_modbus.py` 是仓库 PLC 的 Modbus TCP 客户端，不能直接当作 AGV 原始 TCP 控制程序使用。它会等待 Modbus 应答，而本接收脚本只打印字节。
