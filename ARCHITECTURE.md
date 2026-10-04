# Architecture — SCORM Course Editor

## 1. MVP architecture

```text
Browser UI (Arabic / RTL)
        |
        | HTTP on localhost
        v
Python local server
        |
        +-- ZIP / SCORM detector
        +-- course.json parser
        +-- screen JSON editor API
        +-- question/activity parser
        +-- media resolver + replacement
        +-- structural validator
        +-- incremental ZIP exporter
        |
        v
Original course ZIP  --->  Edited SCORM ZIP
```

The MVP intentionally keeps the original HTML/JavaScript engine intact. It edits only the course data and requested media, reducing the risk of breaking legacy behavior.

## 2. Data model used from the supplied course

```text
Course
  -> Packages / Units
      -> LO/json/course.json
          -> Topics.Topic.pages[]
              -> name
              -> title
              -> type
              -> template
      -> LO/json/<page>.json
          -> video / sync / activity / quiz / other template root
      -> media assets
      -> imsmanifest.xml
```

Known interactive question types:

- choice
- multipleChoice
- matching
- sorting

Unknown templates remain editable through the raw JSON view.

## 3. Recommended production architecture for 52 courses

```text
                         +------------------+
                         |  Web Frontend    |
                         |  Editor/Reviewer |
                         +---------+--------+
                                   |
                         +---------v--------+
                         | Course Editor API |
                         +---+----------+---+
                             |          |
                 +-----------+          +-------------+
                 |                                        |
        +--------v---------+                     +--------v--------+
        | Course Repository|                     | Job / Export     |
        | metadata+versions|                     | Worker           |
        +--------+---------+                     +--------+---------+
                 |                                        |
        +--------v---------+                     +--------v---------+
        | Object Storage   |                     | SCORM Validator  |
        | original/edited  |                     | + packager       |
        +------------------+                     +------------------+
```

### Production modules

1. **Course Library** — import and classify all 52 courses.
2. **Parser adapters** — current legacy JSON engine first; additional engines added as adapters.
3. **Structured editor** — rich text, activities, questions, media, metadata.
4. **Versioning** — original, draft, approved, published; compare and rollback.
5. **Workflow** — Editor -> Reviewer -> Approver.
6. **Validation** — JSON, manifest, missing media, broken references, question answer checks.
7. **Preview sandbox** — run the actual SCORM package safely before export.
8. **Bulk operations** — search/replace across courses, bulk media replacement, bulk export.
9. **Audit trail** — who changed what and when.
10. **Roles** — Admin, Content Editor, Reviewer, Viewer.

## 4. Recommended implementation stack for production

- Frontend: React / Next.js
- API: FastAPI
- Database: PostgreSQL
- File/object storage: S3-compatible storage / MinIO
- Background jobs: Celery or RQ + Redis
- Reverse proxy: Nginx
- Deployment: Docker Compose initially; Kubernetes only if scale justifies it

The course conversion/editing engine should remain a separate Python package so it can be used from the web API, CLI batch jobs, and automated QA tests.
