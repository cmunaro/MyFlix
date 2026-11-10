"""Download notices are committed with publication, never inferred from scans."""
import json


def record_download(db, media_id):
    media = db.execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()
    series, season = media["series"], media["season"]

    def save(key, message):
        db.execute("""INSERT OR IGNORE INTO notifications(root,event_key,media_id,message)
            VALUES(?,?,?,?)""", (media["root"], key, media_id, message))

    message = (f'{series} · S{season:02}E{media["episode"]:02} downloaded'
               if series else f'{media["title"]} downloaded')
    save(f'media:{media_id}', message)
    if not series:
        return
    # A filename cannot prove a whole season is complete. Use the provider's
    # saved episode list and require each listed episode to be visible on disk.
    episodes = db.execute("""SELECT DISTINCT d.episode FROM discoveries d
        JOIN subscriptions s ON s.id=d.subscription_id
        WHERE s.root=? AND s.series=? AND d.season=? AND d.listed=1 AND s.lookup_state='complete'
        ORDER BY d.episode""", (media["root"], series, season)).fetchall()
    if not episodes:
        return
    available = {row[0] for row in db.execute("""SELECT episode FROM media
        WHERE root=? AND series=? AND season=? AND available=1 AND hidden=0""",
        (media["root"], series, season))}
    expected = [row[0] for row in episodes]
    if all(episode in available for episode in expected):
        save('season:' + json.dumps([series, season, expected]),
             f'{series} · Season {season} downloaded')


def download_notices(db, root):
    return [dict(row) for row in db.execute("""SELECT n.id,n.message,n.created_at,m.id media_id,m.series
        FROM notifications n JOIN media m ON m.id=n.media_id
        WHERE n.root=? AND m.hidden=0 AND m.available=1
        ORDER BY n.id DESC LIMIT 50""", (root,))]
