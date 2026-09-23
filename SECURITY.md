# Security Policy

## 凭据与隐私

本项目需要使用者自备平台会话凭证（Cookie / passToken 等）。请务必：

- **不要把任何真实凭据提交到仓库**：`config.yaml`、`.env`、`.env.*`、`secrets/`、
  `*.har`（抓包文件含真实 Cookie）已在 `.gitignore` 中排除，请不要绕过。
- 凭据只保存在本机；`config.yaml` 与 `secrets/` 建议保持 `600` 权限。
- 密码续登功能会在本机保存口令的派生值（`passwordMd5` + bcrypt），请仅在可信机器上使用。
- 服务默认**不开启访问密钥**（`MIMO_API_KEY` 留空）。若部署在可被他人访问的网络中，
  请配置 `MIMO_API_KEY` 或置于反向代理鉴权之后；**不要直接暴露到公网**。

## 报告漏洞

请通过 GitHub 仓库的 **Security → Report a vulnerability**（私有安全通告）提交问题，
不要在公开 issue 中披露可利用细节、真实凭据或个人数据。

报告时请附：受影响版本、复现步骤、影响评估。仅支持**最新发布版本**。
