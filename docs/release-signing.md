# 客户端更新清单的签名与发布

> 面向执行打包发布的人（或 Agent）。流程细节以本文为准，发版的**完整**清单在
> 根目录 `RELEASE-CHECKLIST.md`，这里只讲签名与发布这一段。

## 先理解一件事：签的不是安装包，是清单

客户端更新的信任根是两层，都不依赖分发机：

1. **清单的 Ed25519 签名** —— 版本号、下载地址、哈希、策略这些字段被整体签住
2. **落地字节的 SHA-256** —— 下载完成后逐字节比对

所以安装包放在哪台机器上都不影响安全性。分发机被控制也换不掉内容：换了哈希就
对不上，下载中止，**用户手里的旧版本一个字节都不会动**。

反过来说，**私钥离开签名机的那一刻，签名就不再证明任何事情** —— 拿到它的人能签
出一份客户端会照单全收的恶意更新。它与 `~/Desktop/llano-agent-keys` 同等对待：
不进仓库、不进网关、不进聊天工具。

## 密钥

| | |
| --- | --- |
| 私钥 | `~/llano-signing/update-key.private`（32 字节种子的 base64） |
| 公钥 | `~/llano-signing/update-key.public` |
| 当前公钥 | `1AaTJvXt1VYX/GIPusE4piA71VqMx5wJbQAikeU6x+k=` |
| 指纹 | `2fb78b7edfdccb4a` |

**长期密钥，不随发版更换。** 换它的代价是必须推一次全量安装包 —— 旧客户端里烧的
是旧公钥，新签名它验不过，而它又只能通过更新拿到新公钥。

同一把公钥出现在三处，必须完全一致：

- 客户端**编译期**烧进去：`apps/desktop/product-environments.json` 的
  `updateSigningPublicKey`
- 网关环境变量 `UPDATE_PUBLIC_KEY`
- 签名机上的 `.public`

**签名前先比一次。** 不一致的话签出来的版本谁也装不上，而唯一能看出来的地方是
后台「桌面端版本」页显示的网关公钥指纹。

```bash
cat ~/llano-signing/update-key.public
grep updateSigningPublicKey apps/desktop/product-environments.json
```

## 顺序：先把包放到分发机，再签名

两个理由，第二个更要紧：

1. 签名要写 `artifact_url`，那必须是最终地址
2. **哈希应该对着 IIS 实际下发的字节算**，不是对着构建目录里的文件

第 2 条把 scp、磁盘、MIME 配置这几步一起验了。做法是包上传完之后，从分发地址把
它拉回来，拿拉回来的那份去签：

```bash
curl -o /tmp/sign-src.dmg http://192.0.2.130/releases/LlanoAIWorkbench-<版本>-production-mac-arm64.dmg
```

这样哈希在**四处**比对一致才算通过：构建端产出 → scp 落地分发机 → IIS 实际下发
→ 签名时再算一遍。

## 签名命令

```bash
cd audit-gateway/release
python3 sign_release.py sign \
  --key ~/llano-signing/update-key.private \
  --artifact /tmp/sign-src.dmg \
  --url http://192.0.2.130/releases/LlanoAIWorkbench-<版本>-production-mac-arm64.dmg \
  --version <版本> --platform darwin --arch arm64 \
  --policy optional --min-supported <最低支持版本> \
  --notes-file notes.md \
  --out release-<版本>-darwin-arm64.json
```

- `--platform` 只接受 `darwin` / `win32`；`--arch` 只接受 `x64` / `arm64`
- **每个平台各签一份**：哈希、大小、地址都不同，一份清单不能两用
- `--url` 写**分发机地址**，不是网关地址。网关从不存安装包
- 哈希与大小由工具从 `--artifact` 现算，**不是人填的**
- 工具会**自己验一遍再输出** —— 签完就地验不过说明工具坏了，不会把废签名写出来

历史取值参考：0.17.7 与 0.17.8 都是 `--policy optional --min-supported 0.17.6`。
`min-supported` 停在 0.17.6 是有原因的：那是第一个带分发点白名单的版本，更早的
客户端根本下不动放在 130 上的包。

## 产物

`{manifest, signature}` 两个顶层键。`manifest` 的字段：

```
schema  version  platform  arch  policy  min_supported_version
artifact_url  artifact_sha256  artifact_size  release_notes  released_at
```

## 发布

把整个 JSON 贴进后台「桌面端版本 → 发布新版本」（路由 `releases`）。网关侧
`router/admin/release.py` 会依次做三件事：

1. `normalize_manifest` 校字段
2. **用公钥验签**，不过就拒。错误话术会提示「确认签名机用的私钥与网关公钥是同一对」
3. 查 `(version, platform, arch)` **是否已存在，存在就 409**，提示「要替换请先删除旧条目」

之后每次客户端来问更新，`router/v1/client_update.py` **下发前还会再验一次签**。
验不过就当作「无更新」并打 error 日志 —— 一份验不过的清单不会被发出去。

## 需要重签时：先分清是清单错了还是包错了

**只是清单字段填错（policy / min_supported / notes / url），安装包字节没变**

直接重签、删掉后台旧条目、重新贴。没有风险：`artifact_sha256` 还是同一个，
用户拿到的仍是同一批包。

**安装包重打了（字节变了）**

**必须升版本号，没有例外。** 只要包已经放上分发机，就可能已经有人下走了 ——
尤其是 `/releases/latest/{mac,win}` 这两个固定链接一直指向最新版。

这个坑踩过一次：0.17.6 发过两批，两批都发出去了。拿到第二批的人能自动更新，
拿到第一批的人**永久卡死** —— 他们客户端里没有分发点白名单，而唯一能修好它的
新版本恰恰放在那个分发点上。**自动更新被它自己要防的那条规则挡住了。**

同号不同包还有第二个代价：排障时无法区分用户手里到底是什么，只能靠比对报错文案
的措辞去猜。

## 自己验一遍（贴进后台之前）

```bash
cd audit-gateway/server
python3 - <<'PY'
import json
from services.release_manifest import verify_manifest
pub = open("/Users/developer/llano-signing/update-key.public").read().strip()
d = json.load(open("../release/release-<版本>-darwin-arm64.json", encoding="utf-8"))
m, sig = d["manifest"], d["signature"]
print("验签:", verify_manifest(m, sig, pub))
bad = dict(m); bad["artifact_url"] = m["artifact_url"].replace("25.130", "25.131")
print("改地址后应当失败:", verify_manifest(bad, sig, pub))
PY
```

改 `artifact_size`、`artifact_url`、`policy` 任意一个，验签都必须失败。都失败才
说明签的是整份清单，而不只是某几个字段。

## 分发机上的固定链接

`http://192.0.2.130/releases/latest/mac` 与 `.../latest/win` 是两个 302 跳转，
永远指向当前最新版，配置在 `C:\inetpub\wwwroot\releases\web.config` 的两个
`<location>` 里（需要 IIS 的 `IIS-HttpRedirect` 功能，已启用）。

⚠️ **这两个地址不在自动更新链路上。** 客户端更新走的是网关下发的签名清单里的
`artifact_url`，那是带版本号的固定地址。`latest` 只是给人用的：发链接给同事时
不用每次改版本号。

发新版时**只改那两行的 `destination`**，别的都不用动。改之前先备份 web.config
（那是一个正在对全公司提供下载的站点）。
