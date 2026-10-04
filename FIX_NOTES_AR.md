# إصلاح Windows — WinError 32

تم إصلاح مشكلة كانت تظهر عند فتح مقرر على Windows برسالة شبيهة بـ:

`The process cannot access the file because it is being used by another process [WinError 32]`

## السبب

`tempfile.mkstemp()` ينشئ الملف المؤقت ويُرجع File Descriptor مفتوحًا. النسخة السابقة كانت تحتفظ بهذا الـhandle مفتوحًا ثم تحاول إعادة فتح الملف نفسه للكتابة/المعالجة. Windows يمنع ذلك ويُرجع WinError 32.

## الإصلاح

- إغلاق File Descriptor مباشرة بعد إنشاء الملف المؤقت.
- إعادة فتح الملف فقط داخل `with` context manager ثم إغلاقه قبل تحليل ZIP أو نقله/حذفه.
- تطبيق الإصلاح على الرفع العادي والرفع المجزأ Chunked Upload.

## الاختبار

تم اختبار:

- Direct ZIP upload
- Chunked ZIP upload
- Workspace creation
- Export SCORM
- ZIP integrity بعد التصدير

