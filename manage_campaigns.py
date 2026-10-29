#!/usr/bin/env python3
"""Campaign controls, intentionally local CLI only. Import/preview never sends."""
import argparse
import json
import os
from pathlib import Path
from dotenv import load_dotenv
from campaigns import CampaignStore
from campaign_import import read_contacts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', default='.env')
    parser.add_argument('--db')
    commands = parser.add_subparsers(dest='action', required=True)
    create = commands.add_parser('create')
    create.add_argument('spec')
    imp = commands.add_parser('import')
    imp.add_argument('campaign')
    imp.add_argument('file')
    imp.add_argument('--sheet')
    imp.add_argument('--consent-note', default='', help='Подтверждение оператора о согласии всего списка; заполняет только пустое согласие, не отменяет явный отказ')
    for action in ('preview', 'report', 'activate', 'pause', 'cancel'):
        commands.add_parser(action).add_argument('campaign')
    limits = commands.add_parser('set-limits')
    limits.add_argument('campaign')
    limits.add_argument('--interval-sec', type=int, required=True)
    limits.add_argument('--hourly', type=int, required=True)
    limits.add_argument('--daily', type=int, required=True, help='Лимит за 24 часа; 0 — без суточного лимита')
    export = commands.add_parser('export')
    export.add_argument('campaign')
    export.add_argument('output')
    args = parser.parse_args()
    load_dotenv(args.env_file)
    store = CampaignStore(args.db or os.getenv('CAMPAIGN_STATE_FILE', 'campaigns.sqlite3'))
    if args.action == 'create':
        spec = json.loads(Path(args.spec).read_text())
        store.create(spec)
        result = store.report(spec['id'])
    elif args.action == 'import':
        result = store.import_rows(args.campaign, read_contacts(args.file, args.sheet), consent_note=args.consent_note)
    elif args.action == 'set-limits':
        store.set_limits(args.campaign, args.interval_sec, args.hourly, args.daily)
        result = store.report(args.campaign)
    elif args.action in ('activate', 'pause', 'cancel'):
        if args.action == 'activate' and os.getenv('CAMPAIGNS_ENABLED', '0').lower() not in ('1', 'true', 'yes'):
            raise ValueError('Сначала включите CAMPAIGNS_ENABLED в окружении службы и перезапустите её, затем активируйте кампанию')
        store.control(args.campaign, args.action)
        result = store.report(args.campaign)
    elif args.action == 'export':
        result = store.report(args.campaign)
        with store.db() as db:
            result['recipients'] = [dict(r) for r in db.execute('SELECT * FROM recipients WHERE campaign=?', (args.campaign,))]
            result['replies'] = [dict(r) for r in db.execute('SELECT * FROM replies WHERE campaign=? ORDER BY at', (args.campaign,))]
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as file:
            json.dump(result, file, ensure_ascii=False, indent=2)
        result = {'exported': args.output}
    else:
        result = store.report(args.campaign)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
