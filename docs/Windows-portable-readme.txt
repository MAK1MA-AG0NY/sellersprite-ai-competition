卖家精灵竞品研究工具 · Windows 便携版
===================================

适用：Windows 10/11（64 位，x64）。无需安装 Python 或 Playwright，无需管理员权限。

1. 将 ZIP 完整解压到较短的本地路径，例如把外层文件夹解压到 %LOCALAPPDATA%\Apps。
   不要从网络共享目录或 ZIP 内直接运行，也不要单独移动 EXE；旁边的 _internal 是运行依赖。
2. 双击 SellerSpriteCompetition.exe，等待竞品工作台窗口打开。
   独立窗口使用系统 WebView2；如果不可用，会切换到默认浏览器。
3. 需要使用卖家精灵功能时，在页面上登录自己的主账号或会员账号。
   查竞品详情、广告洞察等仍需对应的卖家精灵权限和网络连接。
4. 使用完毕后关闭工作台窗口，后台服务会随之停止。
   如果进入了浏览器模式，请在“浏览器模式”对话框中点击“确定”退出。

本地数据
--------
- 数据保存在 %LOCALAPPDATA%\SellerSpriteCompetition；报告位于其中的 deep_results。
- 发行包不带账号登录态、业务库、查询历史和报告。首次查询会在本机探测并缓存接口配置。
- 更新时先退出程序，再完整解压新版 ZIP。使用同一 Windows 用户时，软件会复用上述数据目录。
- 不要共享该数据目录中的登录态、数据库或配置文件。

遇到问题
--------
- 提示 8765 端口被占用：先关闭正在运行的旧窗口；也可在 PowerShell 运行
  .\SellerSpriteCompetition.exe --port 8766
- 如果独立窗口不可用，请先确认已解压到用户本地目录，而且系统安装了 Microsoft Edge WebView2 Runtime。
  如果公司策略限制运行，按对话框提示在默认浏览器中使用，或运行：
  .\SellerSpriteCompetition.exe --browser
- 启动日志位于 %LOCALAPPDATA%\SellerSpriteCompetition\logs\desktop.log。
- 公司电脑拦截未知发布者应用：本版未签名，请向内部发布者确认来源并按公司 IT 流程处理。
- FedEx 邮件追踪是可选实验功能，发行包未预置邮箱配置。
