# 仓储 AGV 联动 GUI 使用说明

## 1. 当前版本：第二阶段

本版为 Windows 单机 PySide6 / Qt Widgets 应用，默认进入模拟模式。模拟和真实执行均覆盖 A 库出库、线体与 AGV 上料、AGV 往返、线体与 AGV 下料、A 库入库和退出清理。

新增真实执行适配层，调用现有 `warehouse_agv.deliver()`。只有明确选择“真实设备模式”，点击开始并在参数核对窗口选择“确认连接并启动任务”后，才创建连接并执行设备指令。打开程序、修改模式、保存设置或取消启动核对均不连接设备。每次重新打开程序仍默认模拟模式。

AGV 状态框底边与任务参数框对齐，整数和小数输入框统一使用实心正三角、倒三角按钮。新增原始站点显示，区别于通过准备确认或严格到站检查得到的确认站点。

第二阶段给原控制模块增加可选执行钩子、取消检查与清理报告，保留命令行调用方式、寄存器、16 字节报文、方向和原安全检查。按用户 2026-10-10 的反馈，现场联调与验收已完成。本次按现场反馈调整“停止后退出”及“仅线体出货计时”，代码修改使用离线测试验证。

## 2. 安装与启动

在项目根目录打开 PowerShell，使用已有 Python 3.10 环境：

```powershell
.\agv_control\Scripts\python.exe -m pip install -r requirements.txt
.\agv_control\Scripts\python.exe -m gui.main
```

也可以使用便于双击关联或打包的入口：

```powershell
.\agv_control\Scripts\python.exe run_gui.py
```

依赖保留 `pymodbus==3.11.4`，新增 PySide6。无需启动浏览器、Web 服务或数据库。

## 3. 模拟任务操作

1. 打开“系统设置”，在“控制参数”中显式选择 `word` 或 `pdf` 滚筒定义并保存。首次启动不会替用户选择定义；真实任务须确认现场设备实际采用的指令。
2. 配置出库源库位和入库目标库位，范围为 1～6 列、1～3 层。允许使用同一库位。
3. 输入线体出货时间 `T`，默认 5 秒，支持小数；必须大于 2 秒，且不超过动作超时。该参数只用于出货；线体入货由 PLC 物料检测结束。
4. 用“选择出库位置 / 选择入库位置”切换库位图点击对象。输入框和库位图双向同步；蓝色表示出库选择、绿色表示入库选择、双色及“出 / 入”文字表示相同库位。图中没有真实库存信息。
5. 选择“正常完整流程”，点击“开始模拟任务”。弹出的现场准备确认窗口需要点击“确认并继续模拟”。
6. 未配置物料 IO 时，上料后还会弹出上料完成确认窗口；取消确认会结束任务，不会执行后续模拟行走。
7. 观察八个阶段的颜色、说明、PLC 模拟反馈、AGV 离散站点示意和运行日志。
8. 模拟回库及资源清理结束后，按钮和参数重新解锁，可开始下一次任务。

演示默认采用约 0.15 倍时间比例，保留“上料重叠最后 2 秒、下料重叠最初 2 秒”的业务关系。人工确认等待不按演示比例缩短。设备动作由模拟反馈事件推进；计时结束与物料交接检查分开表达。

站点 1 是装卸交接位置，站点 2 是折返点，不在站点 2 卸货。行走中保留“上次确认站点”，同时显示目标站点和离散行走状态，不展示虚构的连续坐标。速度显示比例，电压显示伏特，不显示推算的电池百分比。

## 4. 取消、失败与关闭

“停止并退出”在待机时直接关闭程序；任务运行时设置线程安全的取消标志，在下一检查点终止后续动作，再执行设备清理。真实任务清理调用 PLC 的原清零/关闭逻辑及 AGV 的三条停止/清目标指令。清理结果产生且后台原生线程退出后，主窗口自动关闭。正在进行的网络请求受通信超时约束，无法瞬间中断；取消检查不会中断后续清理。该按钮不是机械急停。

任务运行和清理期间，任务参数、系统设置和开始按钮锁定，禁止重复启动。清理结束且后台原生线程退出后才解锁。

运行中关闭主窗口会请求取消并关闭待确认窗口，保留 Qt 事件处理直到后台清理和线程退出完成，随后自动关闭主窗口；不强制终止 QThread。

可以通过“模拟场景”验证异常路径：

| 场景 | 演示结果 |
| --- | --- |
| 正常完整流程 | 按完整业务顺序运行并回库 |
| 上料反馈异常 | 上料阶段失败，不执行 AGV 行走 |
| AGV 到站超时 | 前往站点 2 阶段失败，不执行返回、下料或回库 |
| 退出清理异常 | 清理阶段失败，任务不能显示为模拟成功 |

失败或取消时，已完成阶段保留其结果，当前阶段标记失败或取消，未执行阶段保持等待中，清理阶段单独显示结果。下一任务会重置所有阶段。

日志显示时间、级别及消息，区分 INFO、WARNING、ERROR，最多保留 2000 条显示记录；“清空显示”只清空日志窗口，不改变任务状态。本版不自动写入日志文件。

## 5. 设置与代码默认值

设置使用 `QSettings` 的 INI 格式，保存在项目根目录的 `gui_settings.ini`。文件在首次保存设置时创建；设置窗口中显示完整路径。下次启动自动加载。无效设置会回到代码默认值，并通过日志提示重新选择滚筒定义。

连接参数、ID、超时和轮询间隔从现有控制入口读取；GUI 的线体时间 5 秒取自 `outbound_agv.LINE_DURATION`。原完整命令行入口要求用户提供或输入线体时间，GUI 按开发 Prompt 要求显示这个初始值。

可保存 PLC 地址/端口/Slave ID，AGV 地址/端口/ID，滚筒配置，可选物料 IO 与有料电平，查询数据码，动作、行走、就绪、启动、人工确认超时，以及分别配置的 PLC / AGV 通信超时、轮询间隔。物料 IO 与有料电平必须一起配置。

GUI 的真实适配器分别使用 AGV `timeout` / `poll_interval` 与 PLC `plc_io_timeout` / `plc_poll_interval`。默认分别为 5 / 0.5 秒及 3 / 0.2 秒，读取原代码默认值。原完整命令行入口的 `--timeout` / `--poll-interval` 仍只用于 AGV，PLC 使用驱动原默认值。已有第一阶段 INI 可直接加载，缺少的新字段使用代码默认值。

## 6. 模块与事件接口

| 文件 | 职责 |
| --- | --- |
| `gui/main.py` | 创建 QApplication，加载样式和字体，启动窗口 |
| `gui/main_window.py` | 参数、流程、AGV、日志和确认对话框 |
| `gui/storage_grid.py` | 18 个库位和出入库选择同步 |
| `gui/settings.py` | 源码默认值、设置校验、设置对话框和持久化 |
| `gui/models.py` | 任务参数、阶段名称和事件数据契约 |
| `gui/task_controller.py` | 单任务生命周期、取消、确认和线程回收 |
| `gui/task_worker.py` | QObject Worker、线程安全控制、结果和清理汇报 |
| `gui/simulation.py` | 模拟动作、反馈、确认请求和异常场景 |
| `gui/real_backend.py` | 连接真实客户端、调用原调度、转换反馈与 GUI 确认、分别清理两台设备 |
| `gui/styles.qss` | 工业控制台浅色样式 |
| `gui/assets/*.svg` | 上下三角按钮及禁用状态图标 |
| `run_gui.py` | 项目根目录启动入口 |
| `test_gui.py` | Qt offscreen 与后台任务自动化测试 |
| `test_real_backend.py` | 原调度 + 内存 Modbus / 假 AGV 的真实适配器隔离测试 |

后台 Worker 通过 Signal 发送 `TaskEvent`，界面通过 Slot 更新 QWidget。事件包含 `kind`、数据字典、时间和模式标识，类型如下：

| 事件 | 主要内容 |
| --- | --- |
| `stage` | 阶段序号、等待/执行/完成/失败/取消状态和说明 |
| `device` | PLC 与 AGV 状态字段的增量更新 |
| `log` | 日志级别和消息 |
| `confirmation` | 请求 ID、确认类型、标题、说明和等待上限 |
| `cleanup` | 清理成功与否、说明及逐设备报告 |
| `result` | 成功/失败/取消、说明及清理结果和报告 |

`TaskControl` 使用 `threading.Condition`。确认请求在发出事件前注册；主线程用请求 ID 提交结果，防止旧窗口回答新的请求。取消可以唤醒计时或确认等待，不依赖被阻塞的 Worker Qt 事件循环。

线程生命周期遵循 [Qt QThread 文档](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QThread.html)：Worker 移入 QThread，线程结束时删除 Worker；控制器用非阻塞的 `wait(0)` 确认原生线程完全退出后释放引用并解锁。设置文件行为参考 [Qt QSettings 文档](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QSettings.html)。

## 7. 测试运行

在项目根目录执行：

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
.\agv_control\Scripts\python.exe -m unittest -v test_gui test_real_backend test_warehouse_agv test_agv_task test_agv_tcp
.\agv_control\Scripts\python.exe -m compileall -q gui run_gui.py warehouse_agv.py outbound_agv.py warehouse_plc.py test_gui.py test_real_backend.py
.\agv_control\Scripts\python.exe -m pip check
```

完成 offscreen 测试后，恢复桌面模式再启动 GUI：

```powershell
Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue
.\agv_control\Scripts\python.exe -m gui.main
```

GUI 测试覆盖启动、中文字体、参数范围、库位同步、同库位选择、设置持久化、阶段顺序、确认门控、异常反馈、取消、重复启动限制、界面响应和运行中关闭；新增卡片底边对齐、三角按钮点击、真实启动核对取消，以及真实执行路径在 QThread 中完整运行、取消和关闭。真实适配器测试使用内存 Modbus 与假 AGV，仍执行原 `deliver()` 和真实装卸计时算法，使用局部模拟时钟加速。覆盖应答丢失不重发、人工确认期间报警监测、超时、初始站点 0、连接失败及双方独立清理失败。

GUI 和适配器测试禁止真实 socket 创建、Modbus 连接和 `input()`，并断言没有调用。原协议回归测试只使用模拟设备和本地回环服务，不连接现场设备。

测试生成的界面预览在 `tmp/` 下，包括 1366×768、1920×1080、运行中、确认窗口和模拟完成截图。

验证环境为 Python 3.10.11、PySide6 6.12.0 和 pymodbus 3.11.4。当前测试包括停止按钮待机退出、模拟任务清理后退出、真实入货等待期间清理后退出，以及入货完成时间独立于出货 T、入货超时和取消。原有计时断言已按现场反馈调整为“出货计时、入货等待 PLC 完成”。

本次 44 项 GUI 测试、18 项真实适配器测试和 56 项流程/协议测试全部通过，共 118 项；语法编译和依赖检查通过。另使用 Windows 原生 Qt 验证点击停止按钮后完成清理、等待线程退出，并由窗口关闭自然结束事件循环，未强制结束进程或连接现场设备。

另在 Windows 原生 Qt 平台完成启动、中文字体、完整模拟任务、主线程心跳及正常退出的冒烟验证，过程禁止设备连接。截图为 `tmp/gui-native-windows-phase2.png`。

现场联调与验收的完成情况依据用户反馈；本次修改未重新操作现场设备。两种分辨率截图是在 Qt 逻辑像素下检查；其他 Windows DPI 设置仍需在目标电脑上确认。`tmp/gui-real-completed-fake.png` 为假设备执行真实代码路径的截图，不能用作现场任务完成证明。

## 8. 真实设备模式操作与适配接口

先在“系统设置”核对设备地址、ID、滚筒定义、物料 IO、电平、超时及轮询间隔，再输入出入库位置和线体时间。选择“真实设备模式”后点击“开始真实任务”，核对启动窗口列出的 PLC / AGV 地址及任务参数，选择“确认连接并启动任务”。选择模式和取消该核对窗口均不连接设备。

连接后先执行原初始化停止指令，然后弹出现场准备确认。确认源库位有货、AGV 在站点 1 空载且可交接、目标库位在回库时为空后继续。未配置物料 IO 时，上料结束还必须确认货物完全上车、滚筒停止且脱离交接区；未确认不能出发。配置 IO 时保留原装卸前后的电平校验，准备确认仍必需。

出货线体运行 `T` 秒、最后 2 秒滚筒上料；入货线体与 AGV 下料同时启动，滚筒运行 2 秒后停止，线体等待 PLC 检测物料到位并恢复空闲，再清零并进入仓库入库。完整入口通过 `Conveyor.finish_receive()` 调用原 `WarehousePLC.finish()`，复用 `line_inbound()` 的完成握手；`warehouse_modbus.py`、`warehouse_plc.py` 和 `readme.md` 本次保持不变。入货等待仍受动作超时限制，该超时不是线体预定运行时长。

模拟演示时间比例不传给真实适配器。到站仍要求目标站点、零速度、停站/对接完成连续两次反馈，再发送停止并复核。初始原始站点反馈 0 只在现场准备确认后按起点 1 使用；回程站点 0 不会被当作到达站点 1。

程序结束、失败或取消后执行双方清理。报告中的 `travel_stop_ack` / `roller_stop_ack` / `target_clear_ack` 是指令应答；`travel_stationary` 是状态复核结果；`roller_stationary` 和 `plc_mechanical_stop` 为 `None`，表示协议没有证明全部机械停止。PLC 没有待清命令时，`plc_command_clear` 为 `None`，不虚报本次清理发送过清零。任何已尝试的停止/清零或关闭失败都会阻止任务显示成功。错误后不自动恢复搬运，需现场核对后重新准备任务。

当前接口改造：

| 接口 | 改动 |
| --- | --- |
| `warehouse_agv.deliver(..., hooks=None)` | 可选阶段、确认、日志与取消钩子；原四参数调用保持兼容 |
| `outbound_agv.ExecutionHooks` / `execution_hooks()` | 使用 ContextVar 限定于当前执行线程；任务结束恢复原控制台行为 |
| `AGVClient.status_observer` | 复用原状态查询发布 GUI 数据，不新增竞争通信线程 |
| AGV 请求、到站等待、确认等待、装卸计时 | 在请求前及等待循环检查取消，原计时和报文不变 |
| `AGVClient.shutdown_report` | 停止指令应答、停车复核、关闭结果；`shutdown()` 原布尔返回值不变 |
| `WarehousePLC.cancel_check` | 可选读写前检查；清理阶段关闭该检查，保留原 observer 和日志接口 |
| `TaskController.start(..., mode='real', real_confirmed=True)` | 显式确认后选择真实适配器，仍限制单任务 |

全部连接、PLC / AGV 请求及清理都在同一个 Worker 中串行执行。人工确认通过线程安全的有界等待返回原调度，因此等待期间仍查询 AGV；后台不调用终端输入或操作 QWidget。设备卡片仅刷新原调度产生的反馈，某些定时装卸阶段反馈不会连续更新。断开后显示的是最后一次任务反馈。

真实接入继续保留寄存器定义、16 字节帧协议、反转上料/正转下料、严格到站判断、上下料重叠时序，以及不自动重发搬运命令的规则。

## 9. exe 打包建议

本轮优先提供 Python 源码运行，不生成 exe。后续可以先使用 PyInstaller 的目录模式，便于核对 Qt 插件和依赖：

```powershell
.\agv_control\Scripts\python.exe -m pip install pyinstaller
.\agv_control\Scripts\python.exe -m PyInstaller --noconfirm --windowed --onedir --name WarehouseAGV --add-data 'gui/styles.qss;gui' --add-data 'gui/assets;gui/assets' run_gui.py
```

该打包命令为建议，未在本轮执行或验证。打包阶段需将设置文件明确放到用户可写的应用目录，例如 exe 同目录或本地应用数据目录；目前源码使用的路径基于项目目录，不能直接假设打包后仍指向 exe 旁边。现场联调是后续工作，不能由打包测试代替。
