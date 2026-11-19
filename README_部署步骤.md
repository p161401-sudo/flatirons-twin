# Flatirons 数字孪生 · 公网部署包

这个包上传后即可获得一个永久网址，任何电脑点开就能看到活的系统。

## 一、上传到 GitHub

1. 打开 https://github.com/new ，仓库名填 `flatirons-twin`，选 Public，点 Create repository。
2. 在新仓库页面点「uploading an existing file」，把本文件夹里的全部内容拖进去（`app.py`、`site` 文件夹、`requirements.txt`、`Procfile`、`render.yaml`）。
3. 点 Commit changes。上传约 30 MB，需要几分钟。

## 二、在 Render 上一键部署

1. 打开 https://render.com ，用 GitHub 账号登录（免费）。
2. 点 New → Web Service，选择刚建的仓库。
3. 设置：
   - Runtime: Python 3
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `python app.py`
   - Instance Type: Free
4. 点 Create Web Service。等待 2–3 分钟，顶部出现网址，形如 `https://flatirons-twin.onrender.com`。

## 三、验证

打开 `https://你的地址/index.html?lang=zh`，应看到中文三维场站页。
再打开 `https://你的地址/api/status`，应返回 JSON。

## 四、把这个网址放进 PDF / PPT

- PDF：插入超链接，地址填上面的网址。
- PPT：选中文字 → 插入超链接 → 现有文件/网页 → 粘贴网址。

## 注意

- 免费实例闲置 15 分钟后休眠，首次访问需要约 30 秒唤醒，之后正常。
- 站点会在运行时访问 api.weather.gov 获取最新预报，需要出站网络；失败时页面按设计显示“预报已过期”并保留缓存。
- 数据库随仓库一起部署，重启后回到仓库里的状态；演示期间新增的记录不会永久保存。
