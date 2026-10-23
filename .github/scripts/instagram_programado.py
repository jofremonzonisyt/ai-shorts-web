#!/usr/bin/env python3
"""Publica a su hora los Reels de Instagram programados de AI Shorts.

Cada publicación pendiente es un borrador de release con la etiqueta `ig-<episodio>`, creado desde el Mac por
`scripts/publish-api.py --episode <ID> instagram-schedule` del repositorio privado de AI Shorts. Los borradores sólo
los ve quien tiene escritura en este repositorio. El cuerpo del release es un JSON con la hora (`scheduledAt`), el
caption, la cuenta y el estado; los archivos adjuntos son el vídeo y la portada.

Cuando faltan menos de LEAD minutos, el release se hace público para que Instagram pueda descargar los adjuntos.
Después se crea el contenedor, se espera a que Instagram lo procese, se publica a la hora exacta y se borran los
adjuntos. El release vuelve a borrador con el resultado en el cuerpo, para que el Mac lo concilie.

Cada paso se escribe en el cuerpo antes del siguiente: una ejecución interrumpida se retoma sin duplicar el
contenedor ni la publicación. El registro de este repositorio es público: no imprimir secretos, URLs ni caption.
"""
import json, os, sys, time
from datetime import datetime, timezone, timedelta
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPO = os.environ['GITHUB_REPOSITORY']
GH = 'https://api.github.com/repos/' + REPO
GH_TOKEN = os.environ['GH_TOKEN']
IG_TOKEN = os.environ.get('IG_TOKEN', '')
IG = 'https://graph.instagram.com/v26.0/'
WORKFLOW = 'instagram-programado.yml'
LEAD = timedelta(minutes=20)        # Antelación con la que se crea el contenedor.
PROCESS_LIMIT = timedelta(minutes=25)
MAX_RUN = timedelta(hours=5)          # Antes del límite de 6 h por trabajo.
ANNOUNCED = set()
DONE = {'published', 'dry_run_ok', 'error'}


def now():
    return datetime.now(timezone.utc)


def stamp():
    return now().isoformat(timespec='seconds')


def call(url, method='GET', data=None, headers=None):
    req = Request(url, data=data, method=method, headers=headers or {})
    try:
        with urlopen(req, timeout=60) as r:
            body = r.read()
            return r.status, json.loads(body) if body else {}
    except HTTPError as e:
        try:
            detail = json.loads(e.read() or b'{}')
        except ValueError:
            detail = {}
        return e.code, detail


def gh(path, method='GET', body=None):
    status, value = call(GH + path, method, json.dumps(body).encode() if body is not None else None,
                         {'Authorization': 'Bearer ' + GH_TOKEN, 'Accept': 'application/vnd.github+json',
                          'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json'})
    if status >= 300:
        raise RuntimeError('GitHub %s %s: HTTP %s' % (method, path.split('?')[0], status))
    return value


def ig(path, body=None):
    status, value = call(IG + path, 'POST' if body is not None else 'GET',
                         urlencode(body).encode() if body is not None else None,
                         {'Authorization': 'Bearer ' + IG_TOKEN,
                          'Content-Type': 'application/x-www-form-urlencoded'})
    if status >= 300:
        error = (value or {}).get('error', {})
        # Mensaje y código de Meta, sin la URL ni el token.
        raise RuntimeError('Instagram HTTP %s: %s (code %s)' % (status, str(error.get('message', ''))[:160],
                                                                  error.get('code')))
    return value


def save(release, data):
    """Escribe el estado en el cuerpo del release (fuente de verdad entre ejecuciones)."""
    gh('/releases/%s' % release['id'], 'PATCH', {'body': json.dumps(data, ensure_ascii=False, indent=2)})


def make_public(release, data):
    """Publica el release (crea su etiqueta) para que los adjuntos tengan URL pública."""
    if release.get('draft'):
        sha = gh('/commits/' + release.get('target_commitish', 'main'))['sha']
        release = gh('/releases/%s' % release['id'], 'PATCH',
                     {'draft': False, 'tag_name': release['tag_name'], 'target_commitish': sha})
        data['madePublicAt'] = stamp()
        save(release, data)
    return release


def back_to_draft(release, delete_assets):
    if delete_assets:
        for asset in gh('/releases/%s/assets?per_page=100' % release['id']):
            gh('/releases/assets/%s' % asset['id'], 'DELETE')
    gh('/releases/%s' % release['id'], 'PATCH', {'draft': True})
    status, _ = call(GH + '/git/refs/tags/' + release['tag_name'], 'DELETE', None,
                     {'Authorization': 'Bearer ' + GH_TOKEN, 'Accept': 'application/vnd.github+json'})
    if status not in (204, 404, 422):
        raise RuntimeError('No se pudo borrar la etiqueta %s: HTTP %s' % (release['tag_name'], status))


def asset_url(release, name):
    for asset in release.get('assets', []):
        if asset['name'] == name:
            return asset['browser_download_url']
    raise RuntimeError('Falta el adjunto ' + name)


def find_published(data):
    """Si el contenedor ya está publicado, busca la publicación por caption entre las últimas."""
    media = ig('%s/media?fields=id,caption,permalink,timestamp&limit=10' % data['igUserId']).get('data', [])
    for item in media:
        if item.get('caption') == data['caption']:
            return item
    return None


def process(release, data):
    tag = release['tag_name']
    at = datetime.fromisoformat(data['scheduledAt'])
    if now() < at - LEAD:
        if tag not in ANNOUNCED:
            ANNOUNCED.add(tag)
            print('%s: programado, faltan %d min' % (tag, (at - now()).total_seconds() // 60))
        return
    if not IG_TOKEN:
        raise RuntimeError('Falta el secreto IG_TOKEN')
    me = ig('me?fields=user_id,username')
    if str(me.get('user_id')) != str(data['igUserId']) or me.get('username') != data['username']:
        raise RuntimeError('El token no es de la cuenta %s' % data['username'])
    release = make_public(release, data)

    if not data.get('containerId'):
        data['state'] = 'creating_container'
        save(release, data)
        value = ig('%s/media' % data['igUserId'], {
            'media_type': 'REELS', 'video_url': asset_url(release, data['video']),
            'cover_url': asset_url(release, data['cover']), 'caption': data['caption'],
            'share_to_feed': 'true'})
        data.update(containerId=value['id'], containerCreatedAt=stamp(), state='processing')
        save(release, data)
        print('%s: contenedor creado' % tag)

    deadline = now() + PROCESS_LIMIT
    while True:
        status = ig('%s?fields=status_code,status' % data['containerId'])
        code = status.get('status_code')
        if code in ('FINISHED', 'PUBLISHED', 'ERROR', 'EXPIRED') or now() > deadline:
            break
        time.sleep(15)
    data['containerStatus'] = code
    if code in ('ERROR', 'EXPIRED') or code not in ('FINISHED', 'PUBLISHED'):
        raise RuntimeError('Instagram no procesó el Reel: %s %s' % (code, str(status.get('status', ''))[:160]))
    data['containerFinishedAt'] = data.get('containerFinishedAt') or stamp()
    save(release, data)

    if data.get('dryRun'):
        data.update(state='dry_run_ok', checkedAt=stamp())
        save(release, data)
        back_to_draft(release, delete_assets=True)
        print('%s: ensayo correcto, sin publicar' % tag)
        return

    item = find_published(data) if code == 'PUBLISHED' else None
    if not item:
        wait = (at - now()).total_seconds()
        if wait > 0:
            print('%s: listo; espera %d s hasta la hora' % (tag, wait))
            time.sleep(wait)
        data['state'] = 'publishing'
        save(release, data)
        media_id = ig('%s/media_publish' % data['igUserId'], {'creation_id': data['containerId']})['id']
        item = ig('%s?fields=id,caption,permalink,timestamp' % media_id)
    if item.get('caption') != data['caption']:
        raise RuntimeError('El caption publicado no coincide')
    published = datetime.fromisoformat(item['timestamp'].replace('+0000', '+00:00'))
    data.update(state='published', mediaId=item['id'], url=item['permalink'], publishedAt=item['timestamp'],
                delaySeconds=int((published - at).total_seconds()), checkedAt=stamp())
    save(release, data)
    back_to_draft(release, delete_assets=True)
    print('%s: publicado con %d s de diferencia' % (tag, data['delaySeconds']))


def sweep(failed):
    """Procesa lo que toca y devuelve cuántas publicaciones siguen pendientes."""
    pending = 0
    for release in [r for r in gh('/releases?per_page=100') if r['tag_name'].startswith('ig-')]:
        try:
            data = json.loads(release.get('body') or '{}')
        except ValueError:
            failed.append(release['tag_name'] + ': cuerpo no es JSON')
            continue
        if data.get('state') in DONE:
            continue
        try:
            process(release, data)
        except Exception as error:  # Se registra y se sigue con los demás.
            message = str(error) if isinstance(error, RuntimeError) else type(error).__name__
            data.update(state='error', error=message[:300], failedAt=stamp())
            try:
                save(release, data)
                back_to_draft(release, delete_assets=False)
            except Exception:
                pass
            failed.append('%s: %s' % (release['tag_name'], message))
        if data.get('state') not in DONE:
            pending += 1
    return pending


def main():
    # El cron de GitHub es de mejor esfuerzo (se retrasa o se omite): la ejecución espera dentro del trabajo,
    # revisando cada minuto, y antes del límite de 6 h se relanza con workflow_dispatch, que siempre crea una
    # ejecución aunque lo lance el GITHUB_TOKEN. El cron horario queda de respaldo.
    started, failed = now(), []
    while True:
        pending = sweep(failed)
        if not pending:
            if not failed:
                # Nada pendiente: el Mac lo vuelve a activar al programar el siguiente.
                gh('/actions/workflows/%s/disable' % WORKFLOW, 'PUT')
                print('Sin publicaciones pendientes: flujo desactivado')
            break
        if now() - started > MAX_RUN:
            gh('/actions/workflows/%s/dispatches' % WORKFLOW, 'POST', {'ref': 'main'})
            print('Sigue pendiente: relanzado')
            break
        time.sleep(60)
    if failed:
        print('\n'.join('ERROR ' + f for f in failed))
        sys.exit(1)


if __name__ == '__main__':
    main()
