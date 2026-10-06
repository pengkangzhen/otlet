# 发布前真机检查清单（Real-Device Matrix）

CI 绿只证明"在 GitHub 的 windows-latest / macos-latest 虚拟机上能构建"。
以下每格需要**真实设备**上手工过一遍，从 Release 页下载 zip 开始。

## 设备矩阵

| 检查项 | Win10 x64 | Win11 x64 | macOS 14+ (Apple Silicon) |
|--------|-----------|-----------|---------------------------|
| 1 下载→解压→启动 | ☐ | ☐ | ☐ |
| 2 首启无 key 引导 | ☐ | ☐ | ☐ |
| 3 拖入 PDF 导入 + 指纹查重 | ☐ | ☐ | ☐ |
| 4 中文 `grep` 全文命中 | ☐ | ☐ | ☐ |
| 5 `verify` 真实声明（联网） | ☐ | ☐ | ☐ |
| 6 配 key 后 AI 聊天（翻页+活动行） | ☐ | ☐ | ☐ |
| 7 `backup` → 删库 → `restore` | ☐ | ☐ | ☐ |
| 8 `otlet tui` 打开/过滤/详情 | ☐ | ☐ | ☐ |

## 每步的判定标准

1. **启动**：双击可运行；Windows 首次会有 SmartScreen"未知发布者"提示
   （未签名，预期行为——先核对 SHA256SUMS 再点"仍要运行"）；macOS 首启
   右键→打开。启动后不应出现命令行黑框或闪退。
2. **无 key 引导**：聊天框发送任意消息，得到中文配置指引而非堆栈。
3. **导入**：拖 2 个 PDF（其中 1 个重复导入两次）→ 第二次提示
   "Already in library"。
4. **全文**：`otlet grep "任一中文词组"` 命中正确页码（先 `otlet index`）。
5. **核查**：`otlet verify "..." --en "..."` 返回三档判定之一且带证据。
6. **聊天**：回答注明页码范围；终端/界面可见 `→ read_pdf_pages(...)`。
7. **备份恢复**：backup 后删除 `library.db`，restore 后论文数一致，
   `library.db.pre-restore-*` 让位文件存在。
8. **TUI**：方向键、过滤、`:v`/`:c` 不闪退。

## 平台特有注意

- **Windows**：WebView2 运行时缺失时的表现（Win10 LTSC 老镜像可能没有）；
  中文用户名路径（`C:\Users\中文名\...`）导入/备份；OneDrive 同步目录内运行。
- **macOS**：Intel 机型（Rosetta 之外是否可用——当前构建 arm64 + x86 由
  PyInstaller `target_arch=None` 决定，需实测）；外接显示器缩放。

发现问题的回报路径：GitHub Issue（附 `otlet log` 尾部），模板已就位。
