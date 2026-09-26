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

- `local`：把 `useOpenAIKey` 设为 `true`。如果数据库里的 Base URL 是空的，并且旁边的暂存文件里有地址，就把它写回去。如果数据库里已经有地址，就保留，并刷新暂存文件。
- `grok`：把 `useOpenAIKey` 设为 `false`。如果 `openAIBaseUrl` 不是空字符串，先把地址抄到数据库旁边的 `cursor-mode-switch-baseurl.json`（仅当前用户可读），再把字段设成 `null`（和 Cursor 关掉 “Override OpenAI Base URL” 时一样）。暂存失败就不会清空地址。这样 Grok 4.7 / Composer 不再因为自定义 Key / Base URL 报 “This model does not support custom API keys”，地址也不会丢。

写入前，会把 `state.vscdb` 以及存在的 `state.vscdb-wal`、`state.vscdb-shm` 复制到脚本旁边的 `backups/年月日-时分秒/`。已有备份不会被覆盖。命令会打印这个备份目录。输出里不会打印 API Key、token，也不会打印带账号密码的 Base URL。

数据库会自动查找：

- Windows：`%APPDATA%\Cursor\User\globalStorage\state.vscdb`
- macOS：`~/Library/Application Support/Cursor/User/globalStorage/state.vscdb`
- Linux：`~/.config/Cursor/User/globalStorage/state.vscdb`

如果库里没有布尔字段 `useOpenAIKey`，`local` / `grok` 直接退出、不写入。这时 `status` 只列出名称里像 OpenAI、Base URL、API Key、Override 的布尔字段名。

## 它不改什么

- 不删除、不改写已保存的 API Key（`cursorAuth/openAIKey` 或加密行 `secret://cursorAuth/openAIKey`）。
- 不改模型列表里的开/关。那些开关和上面的报错无关。
- 不切换当前模型。`deepseek-v4-flash-vision-exp` 和 Grok 4.7 都不会写进去。
- 不能在 Cursor 设置页面里加按钮。
- 不会打印 API Key 或 Base URL。`status` 只报告 Base URL 是否已设置（yes/no），以及是否有暂存地址（yes/no）。

### Override OpenAI Base URL

这个开关没有单独的布尔字段。只要 `openAIBaseUrl` 是非空字符串，界面就是开的。`grok` 会先把地址写到 `state.vscdb` 同目录的 `cursor-mode-switch-baseurl.json`（不在 git 仓库里，权限只有当前用户），再把数据库里的字段设为 `null`。`local` 会在数据库地址为空时，从这份暂存里把地址放回去。暂存文件写失败时，不会清空数据库里的地址。

### 为什么不改模型

当前模型在 `aiSettings.modelConfig.<界面>.modelName` 和 `selectedModels[].modelId`，还带有该模型自己的 `parameters`。Grok 4.7 的 id 没有写死在 Cursor 3.22.7 里（客户端里能看到的是 `grok-4.6`）。没法证明可以安全写入，所以跳过，不编造模型记录。

## 从备份恢复

1. 完全退出 Cursor（包括托盘）。
2. 打开脚本旁 `backups/` 里对应时间的文件夹。
3. 把里面的 `state.vscdb`、`state.vscdb-wal`、`state.vscdb-shm` 拷回原来的 `globalStorage` 目录，覆盖同名文件。
4. 如果这次备份里没有 `-wal` 或 `-shm`，先删掉目标目录里现有的 `-wal` 和 `-shm`，再只拷回 `state.vscdb`。否则 SQLite 可能用更新的日志把旧库再播一遍。
