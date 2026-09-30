# 图库（官方图复用）

放**现成的官方图**，让胡桃能直接发「本人在场」的画面。

## 为什么需要它

生图模型（通义万相）**不认识「胡桃」**——即使加了专有名词锚定（`tools/image_anchors.json`），
也只能稳定出「红褐色双马尾 + 黑帽子」，刺绣纹样、梅花瞳孔这些细节还原不出来，
本质是「像胡桃的路人」。详见 `docs/输出自然化.md` 第 5.1 节。

官方图则 **100% 保真、0 成本、0 延迟**。所以分工是：

| 场合 | 走哪条路 | 怎么写 |
|---|---|---|
| 画面主体就是她本人（打招呼、情绪、节日） | 图库 | `[[img: 胡桃 立绘]]` |
| 分享见闻、需要具体场景（"今天码头挂灯了"） | 生图 | `[[img_gen: 胡桃在璃月港码头]]` |

## 目录约定

```
gallery/
  <角色名>/
    立绘/     ← 官方立绘、抽卡立绘、节日立绘（日常配图首选）
    表情/     ← 卡牌、名片、头像
    节日/     ← 生日贺图、纪念日贺图
    剧情/     ← 剧情/CG 截图
    动作/     ← 入队/站立动作 GIF（体积大，优先级最低）
  胡桃/       ← 已入库
  钟离/ 魈/ 温迪/ …  ← 其余角色同构
  tags.json   ← 可选：给某张图补额外关键词
```

角色名是**一级关键词**，类别是二级。序号（`_01`）会被忽略。
**目录名和文件名就是标签，不用维护索引。**

## 拉官方图

### 主图源：Enka.Network（推荐，一条命令抓完全阵容）

Enka 镜像的是**游戏内原始素材**，地址可由角色 ID 直接拼出来，**不限流**：

```bash
cd <HUTAO>
python3 tools/fetch_enka.py                    # 抓全部 18 个角色
python3 tools/fetch_enka.py --char 胡桃 甘雨     # 只抓指定角色
python3 tools/fetch_enka.py --dry-run          # 先看计划
```

每个角色最多 4 张：

| 资产 | 内容 | 落到 | 本地名 |
|---|---|---|---|
| `UI_Gacha_AvatarImg_<ID>` | 抽卡立绘（全身大图） | 立绘/ | `<角色>立绘.png` |
| `UI_Gacha_AvatarIcon_<ID>` | 抽卡头像（半身） | 立绘/ | `<角色>半身.png` |
| `UI_AvatarIcon_<ID>` | 圆形头像 | 表情/ | `<角色>头像.png` |
| `UI_NameCardPic_<ID>_P` | 角色名片 | 表情/ | `<角色>名片.png` |

Enka 缺 **白术 / 闲云** 的资产（404），脚本会自动改用 `genshin.jmp.blue` 兜底
（给的是 webp，用 macOS 自带 `sips` 转成 png）。

### 补充源：原神观测枢 wiki（生贺图、活动立绘）

Enka 只有游戏内素材，**没有官方生日贺图、节日贺图**——那些得从 wiki 拿：

```bash
python3 tools/fetch_gallery.py                      # 默认抓胡桃
python3 tools/fetch_gallery.py --char 钟离 --max-kb 7000   # 跳过 6~8MB 的动作 GIF
```

它会自动还原全尺寸原图（缩略图链接去掉 `/thumb` 与尺寸后缀）、按类别归档、
跳过站内小图标与技能图标（`胡桃·安神`／`钟离·天星` 这种是技能/命座图标）。

**⚠️ 会被限流**：wiki 是阿里云 WAF 的 **IP 级封禁**，连刷几页就返回
「请求已被拦截」页（**浏览器同样打不开**，不是 JS 挑战，手动操作救不了）。
实测约 3 页/次、冷却 5~10 分钟。已下好的图会被跳过，分批重跑不会重复下载。

### 手动兜底（wiki 被限流时）

**路 A：浏览器另存页面，脚本继续抓图**（浏览器能过的时候用）

```bash
# 浏览器打开 https://wiki.biligame.com/ys/甘雨 → Cmd+S → 格式选「网页，仅 HTML」
python3 tools/fetch_gallery.py --char 甘雨 --html ~/Downloads/甘雨.html --max-kb 7000
```

图片仍从 `patchwiki.biligame.com` 拉，**CDN 与页面限流相互独立**，实测不受影响。

**路 B：直接右键存图，再导入**

```bash
# 存下来的文件名是 wiki 直链的乱码，脚本会自动改名
python3 tools/import_gallery.py --char 甘雨 --category 立绘 --tag 打招呼 ~/Downloads/*.png
```

`import_gallery.py` 会做三件事：sha256 内容去重（重复导入不产生副本）、
乱码名改成 `<角色><类别><序号>`（带中文的名字原样保留）、`--tag` 的词写进 `tags.json`。
加 `--dry-run` 可先看计划。

图源对照：

| 用途 | 地址 | 备注 |
|---|---|---|
| **游戏内素材（首选）** | `https://enka.network/ui/UI_Gacha_AvatarImg_Hutao.png` | 不限流，ID 见 `tools/fetch_enka.py` 的 `CAST` |
| 生贺/节日贺图 | `https://wiki.biligame.com/ys/胡桃` | 会限流，见上 |
| 官网角色页（手动存图） | `https://ys.mihoyo.com/main/character/liyue?char=5` | |
| 兜底镜像 | `https://genshin.jmp.blue/characters/hu-tao/portrait` | webp，需转 png |

图源（国内直连，无需梯子）：

| 用途 | 地址 |
|---|---|
| 角色 wiki 页（**批量抓取用这个**） | `https://wiki.biligame.com/ys/胡桃` |
| 原神官网角色页（手动右键保存） | `https://ys.mihoyo.com/main/character/liyue?char=5` |
| 国际服官网（需梯子） | `https://genshin.hoyoverse.com/zh-cn/character/liyue?char=5` |

已验证抓到的全尺寸原图：`胡桃立绘.png`（2250×2250）、`胡桃抽卡立绘.png`（2048×1024）、
`生贺·胡桃·2022~2026.png`（2500×2500）、`卡牌-角色牌-胡桃.png`（420×720）。

被自动过滤的两类：`胡桃·安神`／`钟离·天星` 这种 `角色名·词` 是技能/命座图标，
`图书馆导航栏-*` 和 106×106 的缩略图是站内 UI，都不是能发出去的图。

## 手动加图

直接把图片丢进对应类别目录即可，**文件名用中文描述**（会被当成关键词）：

```bash
cp ~/Downloads/xxx.png "gallery/胡桃/立绘/胡桃·新衣装.png"
python3 tools/gallery.py stats     # 确认已入库
```

想给某张图补文件名里没有的关键词，写 `gallery/tags.json`：

```json
{
  "胡桃/立绘/胡桃立绘.png": ["打招呼", "得意", "本堂主"],
  "胡桃/表情/卡牌-角色牌-胡桃.png": ["卡牌", "打牌"]
}
```

## 检索怎么算分

`tools/gallery.py` 的评分规则（查询词越靠左权重越高）：

1. 命中**目录名** +5（`胡桃`、`立绘`、`生贺`）
2. 命中**文件名词** +4（`抽卡`、`卡牌`、`动作`）
3. 宽松子串命中 +2（`打招呼` ↔ `招呼`）
4. 分数打平时按类别优先级裁决：立绘 > 节日 > 剧情 > 表情 > 动作

所以只写「胡桃」会得到立绘，写「胡桃 生贺」才会给生日贺图。
**第一个词是主题词，必须命中**——写「钟离 立绘」而图库没有钟离时会返回 `GALLERY_MISS`，
不会拿「立绘」这一半去匹配、发回一张胡桃图。

文件名里的角色名前缀会被剥掉当关键词：`甘雨/立绘/甘雨半身.png` 除了「甘雨」「立绘」，
还能命中「半身」（中文连写没有分隔符，拆词拆不出来，靠这条补上）。

同一档候选里永远挑**最久没发过**的那张，只要候选 ≥2 张就不会连着发同一张。

手动验证：

```bash
python3 tools/gallery.py pick "胡桃 立绘"   # → GALLERY_OK /绝对/路径.png
python3 tools/gallery.py list              # 列出全部图片及其关键词
python3 tools/gallery.py stats             # 图库统计
```

## 版权说明

官方素材版权归米哈游所有。**图片已被 `.gitignore` 排除，不会进入 git 仓库**——
仓库里只保留本说明、`tags.json` 和目录结构，这样项目仍然是可以公开发布的形式。
各人自行用 `tools/fetch_gallery.py` 在本地拉图。

请勿把这些图用于商业用途或再分发。
