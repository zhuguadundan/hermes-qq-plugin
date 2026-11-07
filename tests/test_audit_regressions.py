"""Regression cases for the 2026-11 reliability audit."""
import asyncio
import time
from pathlib import Path

import pytest
import requests
import threading
from unittest.mock import patch
from hermes_qq.reliability import EventJournal, stream_download

from gateway.config import PlatformConfig
from gateway.platforms.qq import NapCatQQAdapter
from hermes_qq.client import NapCatClient, QQBridgeError
from test_adapter_media_contract import _group_message, _mention_only_adapter


def isolated(tmp_path):
    a, fake, handled = _mention_only_adapter(15)
    a._group_file_root = lambda: tmp_path
    a._group_policy = 'allowlist'
    a._group_allow_from = ['20002']
    return a, fake, handled


def listing(stamp, size=3, name='report.pdf'):
    return {'file_id': f'id-{stamp}', 'file_name': name, 'size': size,
            'modify_time': stamp, 'uploader': 10001, 'uploader_name': 'person'}


def test_new_file_version_is_downloaded(tmp_path):
    a, fake, _ = isolated(tmp_path)
    downloads = []
    def fetch(url, dest, limit):
        dest.write_bytes(b'old' if not downloads else b'NEW')
        downloads.append(dest)
        return 3
    a._fetch_group_file = fetch
    async def run():
        fake.group_files = [listing(int(time.time())-60)]
        await a._backfill_group_files()
        fake.group_files = [listing(int(time.time()))]
        await a._backfill_group_files()
    asyncio.run(run())
    assert len(downloads) == 2
    assert downloads[-1].read_bytes() == b'NEW'
    assert downloads[0] != downloads[1]


def test_concurrent_downloads_use_distinct_paths(tmp_path):
    a, _, _ = isolated(tmp_path)
    chosen = []
    def fetch(url, dest, limit):
        chosen.append(dest)
        dest.write_bytes(b'pdf')
        return 3
    a._fetch_group_file = fetch
    source = a._build_qq_source({'user_id':'10001', 'group_id':'20002'})
    async def run():
        await asyncio.gather(*[
            a._download_group_file(source, {'name':'same.pdf','size':i+3,'time':12345}, f'id{i}', f'key{i}')
            for i in range(2)
        ])
    asyncio.run(run())
    assert len(set(chosen)) == 2
    assert all(p.exists() for p in chosen)


def test_history_commands_are_never_reexecuted(tmp_path):
    a, fake, handled = isolated(tmp_path)
    fake.group_history = [_group_message('/new',message_id='command',at_self=True)]
    asyncio.run(a._backfill_recent_messages())
    assert handled == []


def test_failed_parse_allows_retry(tmp_path):
    a, _, handled = isolated(tmp_path)
    original = a._build_message_parts
    async def fail(*args):
        raise RuntimeError('temporary parsing failure')
    event = _group_message('retry me',message_id='retry',at_self=True)
    async def run():
        a._build_message_parts = fail
        with pytest.raises(RuntimeError):
            await a._handle_onebot_event(event)
        a._build_message_parts = original
        await a._handle_onebot_event(event)
        await asyncio.sleep(0)
    asyncio.run(run())
    assert len(handled) == 1


def test_long_chinese_name_can_be_created(tmp_path):
    a, _, _ = isolated(tmp_path)
    name = '20261107-140000-' + a._safe_file_name('文'*80+'.docx') + '.part'
    path = tmp_path/name
    path.write_bytes(b'pdf')
    assert len(path.name.encode()) <= 255


def test_zero_settings_and_default_limit():
    a = NapCatQQAdapter(PlatformConfig(enabled=True,extra={
        'history_backfill_count':0,'history_backfill_seconds':0,'batch_delay_seconds':0}))
    assert a.history_backfill_count == 0
    assert a.history_backfill_seconds == 0
    assert a._batch_delay_seconds == 0
    assert a.group_file_max_bytes == 200*1048576


def test_disconnect_reaps_file_tasks(tmp_path):
    a, _, _ = isolated(tmp_path)
    async def run():
        task=asyncio.create_task(asyncio.Event().wait())
        scan=asyncio.create_task(asyncio.Event().wait())
        a._group_file_tasks['file']=task
        a._group_file_backfill_task=scan
        await a.disconnect()
        try:
            assert task.done() and scan.done()
        finally:
            task.cancel()
            scan.cancel()
            await asyncio.gather(task,scan,return_exceptions=True)
    asyncio.run(run())


class Response:
    def __init__(self,payload): self.payload=payload
    def raise_for_status(self): pass
    def json(self): return self.payload


def test_nonzero_retcode_is_an_error():
    with pytest.raises(QQBridgeError):
        NapCatClient._parse_response(Response({'retcode':1200,'message':'failed'}),'send_group_msg','path')


def test_send_timeout_is_not_retried_on_another_route():
    client=NapCatClient('http://local.test')
    client._transport_mode='path'
    class Session:
        calls=0
        def post(self,*args,**kwargs):
            self.calls+=1
            if self.calls==1: raise requests.Timeout('response lost after send')
            return Response({'status':'ok','data':{'message_id':42}})
    client.session=Session()
    with pytest.raises(QQBridgeError):
        client.call('send_group_msg',{'group_id':'20002','message':'hello'})
    assert client.session.calls == 1


def test_persistent_dispatch_and_cold_history(tmp_path):
    path=tmp_path/'events.sqlite3'
    a,_,handled=isolated(tmp_path/'files')
    a._journal=EventJournal(path)
    old=_group_message('already before installation',message_id='old',at_self=True)
    old['time']=a._journal.created_at-20
    request=_group_message('new request',message_id='fresh',at_self=True)
    request['time']=time.time()
    async def first():
        await a._handle_onebot_event(old,origin='backfill')
        await a._handle_onebot_event(request)
        await asyncio.sleep(0)
    asyncio.run(first())
    assert len(handled)==1
    a._journal.close()
    b,_,handled2=isolated(tmp_path/'files')
    b._journal=EventJournal(path)
    async def second():
        await b._handle_onebot_event(request,origin='backfill')
        await asyncio.sleep(0)
    asyncio.run(second())
    assert handled2==[]
    b._journal.close()


def test_wait_for_file_does_not_block_reception(tmp_path):
    a,_,handled=isolated(tmp_path)
    async def run():
        entered=asyncio.Event()
        release=asyncio.Event()
        async def download(*args):
            entered.set()
            await release.wait()
            path=tmp_path/'20002'/'report.pdf'
            path.parent.mkdir(parents=True)
            path.write_bytes(b'pdf')
            return str(path),None
        a._download_group_file=download
        await a._handle_onebot_event({'post_type':'notice','notice_type':'group_upload',
            'group_id':'20002','user_id':'10001','time':time.time(),
            'file':{'id':'file','name':'report.pdf','size':3}})
        await entered.wait()
        await asyncio.wait_for(a._handle_onebot_event(_group_message('read it',message_id='mention',at_self=True)),0.1)
        assert not handled
        # Ordinary input remains receivable while the request waits.
        await a._handle_onebot_event(_group_message('later chatter',message_id='later'))
        release.set()
        for _ in range(20):
            await asyncio.sleep(0)
        assert len(handled)==1
        assert str(tmp_path/'20002'/'report.pdf') in handled[0].text
        assert 'later chatter' not in handled[0].text
        assert 'later chatter' in a._render_passive_context('20002')
    asyncio.run(run())


def test_timeout_keeps_pending_file_status_queryable(tmp_path):
    a,_,handled=isolated(tmp_path)
    a.group_file_wait_seconds=0
    async def run():
        release=asyncio.Event()
        async def download(*args):
            await release.wait()
            return str(tmp_path/'late.pdf'),None
        a._download_group_file=download
        await a._handle_onebot_event({'post_type':'notice','notice_type':'group_upload',
            'group_id':'20002','user_id':'10001','time':time.time(),
            'file':{'id':'file','name':'late.pdf','size':3}})
        await a._handle_onebot_event(_group_message('read it',message_id='mention',at_self=True))
        for _ in range(10): await asyncio.sleep(0)
        assert '正在下载' in handled[0].text
        release.set()
        await asyncio.gather(*list(a._group_file_tasks.values()))
    asyncio.run(run())


def test_recursive_enumeration_and_cycle_guard(tmp_path):
    a,fake,_=isolated(tmp_path)
    def root(group):
        return {'files':[listing(1)],'folders':[{'folder_id':'child'}]}
    def child(group,folder):
        return {'files':[listing(2,name='nested.pdf')],'folders':[{'folder_id':'child'}]}
    fake.get_group_root_files=root
    fake.get_group_files_by_folder=child
    files=asyncio.run(a._list_group_files('20002'))
    assert len(files)==2
    assert files[1]['folder_id']=='child'
    assert a._group_file_key('20002','a',3,'child') != a._group_file_key('20002','a',3)


def test_directory_request_explicitly_raises_listing_limit():
    c=NapCatClient('http://local.test')
    calls=[]
    c.call=lambda action,params: calls.append((action,params)) or {'data':{}}
    c.get_group_root_files('20002')
    c.get_group_files_by_folder('20002','child')
    assert all(p['file_count']==10000 for _,p in calls)
    assert calls[1][1]['folder_id']=='child'


def test_pending_bytes_are_reserved_without_disabling_retention(tmp_path):
    a,_,_=isolated(tmp_path)
    a.group_file_cache_max_bytes=10
    group=tmp_path/'20002'
    group.mkdir()
    old=group/'old.pdf'
    old.write_bytes(b'123456')
    a._group_file_index={'old':{'path':str(old),'name':'old.pdf','size':6,'src_mtime':100,'group_id':'20002'}}
    a._download_reservations[str(group/'new.pdf')]=6
    assert a._enforce_cache_size_cap()
    assert not old.exists()
    assert a._group_file_index['old']['reason']=='size-cap'
    (group/'active.pdf.part').write_bytes(b'x'*11)
    assert not a._enforce_cache_size_cap()


def test_archive_hint_is_independent_of_chat_buffer(tmp_path):
    a,_,_=isolated(tmp_path)
    (tmp_path/'20002').mkdir()
    assert str(tmp_path/'20002') in a._render_passive_context('20002')
    assert not a._passive_backlog


@pytest.mark.parametrize('payload,limit',[([b'123',b'456'],5),([b'abc'],3)])
def test_stream_limit_and_atomic_file(tmp_path,payload,limit):
    class Stream:
        headers={}
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def raise_for_status(self): pass
        def iter_content(self,size): return iter(payload)
    dest=tmp_path/'file.pdf'
    with patch('hermes_qq.reliability.requests.get',return_value=Stream()):
        if sum(map(len,payload)) > limit:
            with pytest.raises(ValueError):
                stream_download('http://local.test',dest,limit,5,threading.Event())
            assert not dest.exists() and not dest.with_suffix('.pdf.part').exists()
        else:
            assert stream_download('http://local.test',dest,limit,5,threading.Event())==limit
            assert dest.read_bytes()==b''.join(payload)


def test_shutdown_flag_prevents_more_downloads(tmp_path):
    stop=threading.Event()
    stop.set()
    with pytest.raises(InterruptedError):
        stream_download('http://unused',tmp_path/'file',200,5,stop)


@pytest.mark.parametrize('name,value',[('group_file_max_mb',-1),('history_backfill_count',float('nan')),
    ('batch_delay_seconds',-2),('group_file_concurrency',0)])
def test_invalid_config_fails_explicitly(name,value):
    with pytest.raises(ValueError):
        NapCatQQAdapter(PlatformConfig(enabled=True,extra={name:value}))


def test_same_file_from_two_uploaders_is_not_merged(tmp_path):
    a,fake,_=isolated(tmp_path)
    downloads=[]
    def fetch(url,dest,limit):
        downloads.append(dest)
        dest.write_bytes(b'pdf')
        return 3
    a._fetch_group_file=fetch
    info=listing(int(time.time()))
    fake.group_files=[info,{**info,'uploader':10002,'file_id':'other-id'}]
    async def run():
        await a._backfill_group_files()
        await a._backfill_group_files()
    asyncio.run(run())
    assert len(downloads)==2
    assert len(a._group_file_index)==2


def test_older_versions_stay_addressable_without_redownload(tmp_path):
    a,fake,_=isolated(tmp_path)
    downloads=[]
    def fetch(url,dest,limit):
        downloads.append(dest)
        dest.write_bytes(b'pdf')
        return 3
    a._fetch_group_file=fetch
    fake.group_files=[listing(int(time.time())),listing(int(time.time())-60)]
    async def run():
        await a._backfill_group_files()
        await a._backfill_group_files()
    asyncio.run(run())
    assert len(downloads)==2
    assert len(set(downloads))==2
    assert len(a._group_file_index)==2


def test_scan_and_notice_share_one_download(tmp_path):
    a,fake,_=isolated(tmp_path)
    calls=[]
    def fetch(url,dest,limit):
        calls.append(url)
        dest.write_bytes(b'pdf')
        return 3
    a._fetch_group_file=fetch
    source=a._build_qq_source({'user_id':'10001','group_id':'20002'})
    data={'name':'report.pdf','size':3,'modify_time':100}
    async def run():
        paths=await asyncio.gather(*[a._download_group_file(source,data,'f1','key') for _ in range(3)])
        assert len({path for path,note in paths})==1
    asyncio.run(run())
    assert len(calls)==1


def test_stream_does_not_delete_someone_elses_part(tmp_path):
    part=tmp_path/'file.part'
    part.write_bytes(b'owned by another worker')
    class Stream:
        headers={}
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def raise_for_status(self): pass
        def iter_content(self,size): return iter([b'new'])
    with patch('hermes_qq.reliability.requests.get',return_value=Stream()):
        with pytest.raises(FileExistsError):
            stream_download('http://local.test',tmp_path/'file',200,5,threading.Event())
    assert part.read_bytes()==b'owned by another worker'


def test_incomplete_core_install_is_reported(tmp_path):
    import importlib.util
    script=Path(__file__).parents[1]/'scripts'/'patch_hermes_core.py'
    spec=importlib.util.spec_from_file_location('qq_patch',script)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert len(module.verify_core(tmp_path))==6


def test_processed_event_state_is_pruned(tmp_path):
    journal=EventJournal(tmp_path/'events.sqlite3')
    journal.db.execute('INSERT INTO events VALUES (?,?)',('old',time.time()-8*86400))
    journal.db.commit()
    journal.finish(['new'])
    journal.prune()
    assert not journal.contains('old') and journal.contains('new')
    journal.close()


def test_failed_media_with_text_is_retryable(tmp_path):
    a,_,handled=isolated(tmp_path)
    event=_group_message('look at this',message_id='image',at_self=True)
    event['message'].append({'type':'image','data':{'file':'unavailable'}})
    async def no_media(*args): return None
    a._resolve_media=no_media
    async def run():
        with pytest.raises(RuntimeError):
            await a._handle_onebot_event(event)
    asyncio.run(run())
    assert not handled and not a._seen_events and not a._event_inflight


def test_gateway_fallback_does_not_resend_unknown_qq_result(tmp_path):
    a,fake,_=isolated(tmp_path)
    calls=[]
    def ambiguous(*args):
        calls.append(args)
        raise QQBridgeError('send result unknown')
    fake.send_text=ambiguous
    result=asyncio.run(a._send_with_retry('group:20002','hello',max_retries=2,base_delay=0))
    assert not result.success
    assert len(calls)==1
