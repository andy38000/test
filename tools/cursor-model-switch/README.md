# Cursor 本机模式切换

在你自己的电脑上运行。它改的是这台电脑上 Cursor 的用户数据，不会改这个仓库，也不能从别的机器上改你正在打开的 Cursor。

它不能在 Cursor 设置里加一个按钮。要切换，就运行下面的一条命令。

## 运行前

先完全退出 Cursor，包括系统托盘里的图标。Cursor 还在运行时，工具会拒绝写入，并且不会结束 Cursor 进程。

## 一条命令一个模式

进入 `tools/cursor-model-switch` 目录。

Windows：

```bat
switch-local.bat
switch-grok.bat
```

`switch-local.bat` / `switch-grok.bat` 使用 `py -3`，找不到时再调用 `python`。

查看状态：

```bat
py -3 cursor_mode_switch.py status
```

macOS / Linux：

```bash
python3 cursor_mode_switch.py local
python3 cursor_mode_switch.py grok
python3 cursor_mode_switch.py status
```

## 它改什么

只改全局库 `globalStorage/state.vscdb`（SQLite 表 `ItemTable`）里这一行 JSON 的一个布尔值：

`src.vs.platform.reactivestorage.browser.reactiveStorageServiceImpl.persistentStorage.applicationUser`

字段是 `useOpenAIKey`，对应设置里的 “Use OpenAI API Key”。依据是 Cursor 3.22.7 的界面代码。打开数据库时按 SQLite 的正常方式读取，因此会带上旁边的 `-wal`。

- `local`：把 `useOpenAIKey` 设为 `true`，继续用已经保存的 Key 和 Base URL。
- `grok`：把 `useOpenAIKey` 设为 `false`，避免 Grok 4.7 / Composer 报 “This model does not support custom API keys”。

写入前，会把 `state.vscdb` 以及存在的 `state.vscdb-wal`、`state.vscdb-shm` 复制到脚本旁边的 `backups/年月日-时分秒/`。已有备份不会被覆盖。命令会打印这个备份目录。输出里不会打印 API Key、token，也不会打印带账号密码的 Base URL。

数据库会自动查找：

- Windows：`%APPDATA%\Cursor\User\globalStorage\state.vscdb`
- macOS：`~/Library/Application Support/Cursor/User/globalStorage/state.vscdb`
- Linux：`~/.config/Cursor/User/globalStorage/state.vscdb`

如果库里没有布尔字段 `useOpenAIKey`，`local` / `grok` 直接退出、不写入。这时 `status` 只列出名称里像 OpenAI、Base URL、API Key、Override 的布尔字段名。

## 它不改什么

- 不删除、不改写已保存的 API Key（`cursorAuth/openAIKey` 或加密行 `secret://cursorAuth/openAIKey`）。
- 不删除、不改写 `openAIBaseUrl` 这串地址。
- 不改模型列表里的开/关。那些开关和上面的报错无关。
- 不切换当前模型。`deepseek-v4-flash-vision-exp` 和 Grok 4.7 都不会写进去。
- 不能在 Cursor 设置页面里加按钮。

### 为什么不关 “Override OpenAI Base URL”

这个开关没有单独的布尔字段。界面是否打开，只看 `openAIBaseUrl` 是不是非空字符串。关掉它时，Cursor 会把该字段写成 `null`，已保存的地址就没了。本工具要留下这串地址，所以不动它。

切到 `grok` 之后，如果原来保存过地址，设置里的 Override 开关仍会显示为开。`useOpenAIKey` 关掉后，通常就不会再报 “does not support custom API keys”。如果请求仍被送到自定义地址，需要在设置里手动关掉 Override（这会清空地址；可以先用备份找回）。

### 为什么不改模型

当前模型在 `aiSettings.modelConfig.<界面>.modelName` 和 `selectedModels[].modelId`，还带有该模型自己的 `parameters`。Grok 4.7 的 id 没有写死在 Cursor 3.22.7 里（客户端里能看到的是 `grok-4.6`）。没法证明可以安全写入，所以跳过，不编造模型记录。

## 从备份恢复

1. 完全退出 Cursor（包括托盘）。
2. 打开脚本旁 `backups/` 里对应时间的文件夹。
3. 把里面的 `state.vscdb`、`state.vscdb-wal`、`state.vscdb-shm` 拷回原来的 `globalStorage` 目录，覆盖同名文件。
4. 如果这次备份里没有 `-wal` 或 `-shm`，先删掉目标目录里现有的 `-wal` 和 `-shm`，再只拷回 `state.vscdb`。否则 SQLite 可能用更新的日志把旧库再播一遍。
