# aria2
Download Or Upload

Ubuntu সার্ভারে **এক কমান্ডে** aria2 + AriaNg (ডাউনলোড প্যানেল) + File Browser (আপলোড/ডাউনলোড প্যানেল) সেটআপ।

## ইনস্টল

```bash
curl -fsSL https://raw.githubusercontent.com/abnet-rakib-123/aria2/main/install.sh | sudo bash
```

ডোমেইন + HTTPS সহ:

```bash
curl -fsSL https://raw.githubusercontent.com/abnet-rakib-123/aria2/main/install.sh | sudo DOMAIN=example.com EMAIL=you@example.com bash
```

শেষে স্ক্রিনে লিংক, টোকেন আর পাসওয়ার্ড দেখাবে। এগুলো `/root/aria2-web-credentials.txt` ফাইলেও সেভ থাকবে।

## অপশন (environment variable)

| ভেরিয়েবল | কাজ | ডিফল্ট |
|---|---|---|
| `DOMAIN` | ডোমেইন দিলে Let's Encrypt দিয়ে HTTPS চালু হবে | নেই (শুধু IP + HTTP) |
| `EMAIL` | Let's Encrypt এর জন্য ইমেইল (`DOMAIN` দিলে লাগবেই) | নেই |
| `DOWNLOAD_DIR` | ফাইল কোন ফোল্ডারে থাকবে | `/srv/downloads` |
| `RPC_SECRET` | aria2 টোকেন | নিজে থেকে তৈরি হবে |
| `FB_PASSWORD` | File Browser এর admin পাসওয়ার্ড | নিজে থেকে তৈরি হবে |

## ব্যবহার

- `http://SERVER-IP/` → AriaNg: লিংক, টরেন্ট বা ম্যাগনেট দিয়ে ডাউনলোড
- `http://SERVER-IP/files` → File Browser: আপলোড, পিসিতে নামানো, rename, delete, শেয়ার লিংক

## আনইনস্টল

```bash
sudo bash install.sh --uninstall
```

(ডাউনলোড করা ফাইলগুলো মুছবে না।)
