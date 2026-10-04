# تشغيل SCORM Course Editor عبر الإنترنت

هذه النسخة تدعم Cloudflare Quick Tunnel للاختبار والوصول المؤقت من خارج الجهاز.

## المتطلبات

- Python 3
- `cloudflared`

macOS:

```bash
brew install cloudflared
```

## تشغيل عام بضغطة واحدة

macOS / Linux:

```bash
./run_public_mac_linux.sh
```

Windows:

```text
run_public_windows.bat
```

سيظهر في Terminal/Command Prompt:

- رابط HTTPS عام من `trycloudflare.com`
- اسم المستخدم `editor`
- كلمة مرور عشوائية جديدة لكل تشغيل

أبقِ نافذة Terminal مفتوحة. عند الضغط على `Ctrl+C` يتوقف السيرفر والتونل ويصبح الرابط غير متاح.

## الملفات الكبيرة

المحرر يرفع ملفات ZIP على أجزاء بحجم يقارب 50MB، حتى لا يفشل رفع المقررات الكبيرة بسبب حدود حجم طلب HTTP في الـreverse proxy.

## مهم

Quick Tunnel مناسب للاختبار وليس للنشر الدائم. للنشر المؤسسي استخدم Cloudflare Named Tunnel مع نطاق ثابت وCloudflare Access/SSO.
