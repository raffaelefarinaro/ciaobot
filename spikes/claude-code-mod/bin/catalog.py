# Reads the Ciao workspace read-only and prints the catalog the pane needs.
# web_projects.json sits near the 4 MiB $.fs limit, so Python projects it down.
import json, os, sys
root = sys.argv[1]
rt = os.path.join(root, '.runtime')
ws = json.load(open(os.path.join(rt, 'workspaces.json')))
data = json.load(open(os.path.join(rt, 'web_projects.json')))
roots = {w['name']: w.get('vault_root', '') for w in ws}
chats = {}
for c in data.get('chats', {}).values():
    if c.get('archived'):
        continue
    chats.setdefault(c.get('project_id'), []).append(c)
projects = []
for pid, p in data.get('projects', {}).items():
    if p.get('kind') == 'memory':
        continue
    folder = p.get('vault_folder') or ''
    doc = ''
    if folder:
        base = os.path.join(root, roots.get(p.get('workspace'), ''), 'projects', 'active', folder)
        for cand in (os.path.join(base, 'README.md'), base + '.md', os.path.join(base, folder + '.md')):
            if os.path.isfile(cand):
                doc = cand
                break
    mine = sorted(chats.get(pid, []), key=lambda c: c.get('last_activity_at') or '', reverse=True)
    projects.append({
        'id': pid, 'name': p.get('name', pid), 'workspace': p.get('workspace', ''),
        'context': ' '.join((p.get('context') or '').split())[:1200],
        'canonicalDoc': doc, 'order': p.get('order', 0),
        'chatCount': len(mine), 'recentChats': [c.get('title') or 'Untitled' for c in mine[:3]],
    })
projects.sort(key=lambda p: (p['workspace'], p['name'] != 'General', p['order'], p['name'].lower()))
print(json.dumps({
    'root': root,
    'workspaces': [{'name': w['name'], 'vaultRoot': os.path.join(root, w.get('vault_root', '')),
                    'gwsProfile': w.get('gws_profile') or '', 'color': w.get('color') or ''} for w in ws],
    'projects': projects,
}))
