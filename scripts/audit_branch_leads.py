#!/usr/bin/env python3
"""Read-only audit of legacy bot enrollment into the shared Arkada class.

Writes a private review plan and snapshots. Never modifies CRM or sends messages.
Run with the project environment; review every proposed deletion before repair.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
import logging

import httpx
from dotenv import load_dotenv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--class-id', type=int, default=341820)
    args = parser.parse_args()
    load_dotenv(args.env_file)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from bot import FILIALS_MAP
    logging.getLogger('httpx').setLevel(logging.WARNING)

    base = 'https://api.moyklass.com/v1/company'
    with httpx.Client(timeout=30) as client:
        auth = client.post(base + '/auth/getToken', json={'apiKey': os.environ['MOYKLASS_API_KEY']})
        auth.raise_for_status()
        headers = {'x-access-token': auth.json()['accessToken']}

        def get(path, **params):
            # Leave API capacity for the live bot; retry rate limits read-only.
            for attempt in range(6):
                time.sleep(1.6)
                resp = client.get(base + path, headers=headers, params=params)
                if resp.status_code != 429:
                    break
                time.sleep(min(30, 5 * (attempt + 1)))
            resp.raise_for_status()
            return resp.json()

        classes = {}

        def group(class_id):
            if class_id not in classes:
                classes[class_id] = get('/classes/' + str(class_id))
            return classes[class_id]

        source = group(args.class_id)
        source_branch = source['filialId']
        joins = []
        offset = 0
        while True:
            batch = get('/joins', classId=args.class_id, limit=500, offset=offset)['joins']
            joins.extend(batch)
            if len(batch) < 500:
                break
            offset += len(batch)

        candidates, review, counts, users = [], [], Counter(), {}
        for join in joins:
            comment = join.get('comment') or ''
            branch = re.search(r'^Филиал:\s*([^\n]+)', comment, re.M)
            if not comment.startswith('BOT ЗАЯВКА:') or not branch:
                continue
            counts[branch.group(1)] += 1
            targets = {fid for alias, fid in FILIALS_MAP.items() if alias in branch.group(1).lower()}
            if targets == {source_branch}:
                continue
            entry = {'join': join, 'requested_branch': branch.group(1)}
            if len(targets) != 1:
                review.append({**entry, 'reason': 'ambiguous_target'})
                continue
            target = next(iter(targets))
            uid = join['userId']
            if uid not in users:
                users[uid] = get('/users/' + str(uid), includeJoins='true')
            user = users[uid]
            stats = join.get('stats') or {}
            records = get('/lessonRecords', userId=uid, classId=args.class_id, limit=1)
            if 'lessonRecords' not in records or not stats or records.get('lessonRecords') or join.get('invoices') or any(
                stats.get(key) for key in ('visits', 'freeVisits', 'nextRecord', 'lastVisit',
                                          'lessonsLost', 'totalPayed', 'nonPayedLessons')):
                review.append({**entry, 'reason': 'has_lessons_or_financial_history'})
                continue
            other_source = [j for j in user.get('joins', []) if j['id'] != join['id']
                            and group(j['classId'])['filialId'] == source_branch]
            filials = user.get('filials') or []
            if target not in filials:
                review.append({**entry, 'reason': 'target_missing_from_card'})
                continue
            patch = {}
            if not other_source and source_branch in filials:
                patch['filials'] = [f for f in filials if f != source_branch]
                if user.get('mainFilialId') == source_branch:
                    patch['mainFilialId'] = target
            candidates.append({**entry, 'user_id': uid, 'target_filial_id': target,
                               'delete_join_id': join['id'], 'user_patch': patch,
                               'keeps_source_branch': bool(other_source)})
        report = {'created_at': datetime.now(timezone.utc).isoformat(), 'read_only': True,
                  'source_class': source, 'total_joins': len(joins), 'bot_branch_counts': counts,
                  'candidates': candidates, 'manual_review': review,
                  'user_snapshots': users, 'class_snapshots': classes}
        output = Path(args.output)
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as file:
            json.dump(report, file, ensure_ascii=False, indent=2)
        output.chmod(0o600)
        print(json.dumps({'total_joins': len(joins), 'candidate_joins': len(candidates),
                          'candidate_users': len({c['user_id'] for c in candidates}),
                          'branch_removals': sum(bool(c['user_patch']) for c in candidates),
                          'manual_review': len(review), 'output': str(output)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
