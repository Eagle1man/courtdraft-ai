"""Сбор метрик теста со стенда в JSON (только чтение).

Складывает в JSON объёмы, качество проверок, отзывы и таблицу дел.
Удобно для разбора: цифры не нужно считать руками.

Запуск:
    python scripts\\diagnostics\\collect_metrics.py
    python scripts\\diagnostics\\collect_metrics.py out.json
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile

import paramiko

sys.stdout.reconfigure(errors="replace")

HOST = os.environ.get("COURTDRAFT_VM_HOST", "10.19.20.20")
USER = os.environ.get("COURTDRAFT_VM_USER", "ubuntuii")
KEY = pathlib.Path(
    os.environ.get("COURTDRAFT_VM_KEY", str(pathlib.Path.home() / ".ssh" / "id_ed25519_lecture"))
)
DEFAULT_OUT = pathlib.Path(tempfile.gettempdir()) / "courtdraft_metrics.json"
LOCAL = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT

PY = r"""
import json, sqlite3

db = r'/home/ubuntuii/courtdraft-ai/data/cases/courtdraft.db'
con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
con.row_factory = sqlite3.Row

def one(sql, args=()):
    row = con.execute(sql, args).fetchone()
    return row[0] if row else None

def rows(sql, args=()):
    return [dict(r) for r in con.execute(sql, args)]

SINCE = '2026-11-03 08:43:00'   # после прошлого разбора

out = {}
out['totals'] = {
    'cases': one('select count(*) from cases'),
    'visible': one('select count(*) from cases where is_archived = 0'),
    'archived': one('select count(*) from cases where is_archived = 1'),
    'documents': one('select count(*) from case_documents'),
    'documents_chars': one('select sum(length(text)) from case_documents'),
    'drafts': one('select count(*) from drafts'),
    'versions': one('select count(*) from draft_versions'),
    'verifications': one('select count(*) from verifications'),
    'feedback': one('select count(*) from feedback'),
}
out['since_last'] = {
    'cases': one('select count(*) from cases where created_at > ?', (SINCE,)),
    'documents': one('select count(*) from case_documents where created_at > ?', (SINCE,)),
    'drafts': one('select count(*) from drafts where created_at > ?', (SINCE,)),
    'verifications': one('select count(*) from verifications where created_at > ?', (SINCE,)),
    'feedback': one('select count(*) from feedback where created_at > ?', (SINCE,)),
}
out['timeline'] = {
    'first_case': one('select min(created_at) from cases'),
    'last_case': one('select max(created_at) from cases'),
    'last_document': one('select max(created_at) from case_documents'),
    'last_draft': one('select max(created_at) from drafts'),
    'last_feedback': one('select max(created_at) from feedback'),
}

out['codes'] = rows(
    "select coalesce(student_code,'<пусто>') as code, count(*) as cases "
    "from cases where is_archived = 0 group by 1 order by cases desc")
out['doc_types'] = rows(
    "select document_type, count(*) as n, cast(avg(length(text)) as int) as avg_chars "
    "from case_documents group by 1 order by n desc")

out['verification'] = {
    'by_status': rows("select status, count(*) as n from verifications group by 1"),
    'claims_sum': one('select sum(claims_checked) from verifications'),
    'unsupported_sum': one('select sum(unsupported_claims) from verifications'),
    'avg_claims': one('select cast(avg(claims_checked) as real) from verifications'),
    'avg_unsupported': one('select cast(avg(unsupported_claims) as real) from verifications'),
}

kinds = {}
for row in con.execute('select issues from verifications'):
    try:
        issues = json.loads(row['issues']) if isinstance(row['issues'], str) else (row['issues'] or [])
    except Exception:
        issues = []
    for item in issues:
        if isinstance(item, dict):
            key = item.get('type') or item.get('kind') or 'other'
            sev = item.get('severity') or ''
            label = f"{key}|{sev}" if sev else str(key)
            kinds[label] = kinds.get(label, 0) + 1
out['issue_kinds'] = sorted(
    ({'kind': k, 'n': v} for k, v in kinds.items()), key=lambda x: -x['n'])

out['feedback'] = rows(
    "select rating, verdict, coalesce(author_role,'') as role, "
    "coalesce(model_name,'') as model, substr(coalesce(comment,''),1,400) as comment, "
    "created_at from feedback order by created_at desc")

out['cases_table'] = rows('''
    select c.case_number, coalesce(c.student_code,'') as code,
           (select count(*) from case_documents d where d.case_id = c.id) as docs,
           (select count(*) from drafts x where x.case_id = c.id) as drafts,
           (select count(*) from verifications v join drafts x on x.id = v.draft_id
             where x.case_id = c.id) as checks,
           c.created_at
    from cases c where c.is_archived = 0 order by c.created_at desc
''')

out['audit'] = rows(
    "select action, count(*) as n from audit_entries group by 1 order by n desc")

out['budget_hint'] = {
    'cases_without_draft': one(
        "select count(*) from cases c where c.is_archived = 0 and not exists "
        "(select 1 from drafts d where d.case_id = c.id)"),
    'cases_without_docs': one(
        "select count(*) from cases c where c.is_archived = 0 and not exists "
        "(select 1 from case_documents d where d.case_id = c.id)"),
}
print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
"""


def main() -> None:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        HOST,
        username=USER,
        pkey=paramiko.Ed25519Key.from_private_key_file(str(KEY)),
        timeout=20,
        look_for_keys=False,
        allow_agent=False,
    )
    sftp = client.open_sftp()
    with sftp.open("/tmp/_live.py", "w") as handle:
        handle.write(PY)
    sftp.close()
    _, stdout, stderr = client.exec_command(
        "cd /home/ubuntuii/courtdraft-ai/backend && .venv/bin/python /tmp/_live.py",
        timeout=300,
    )
    code = stdout.channel.recv_exit_status()
    data = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace").strip()
    client.close()
    if code != 0:
        print("ошибка:", err[:800])
        raise SystemExit(1)
    LOCAL.write_text(data, encoding="utf-8")
    print("сохранено:", LOCAL)


if __name__ == "__main__":
    main()
