'use strict';

// Read the logged-in client's existing SQLite connection. Never open a file,
// extract database keys, advance a message cursor, or execute caller SQL.
function createNativeMessageStore(accountDetails) {
  const dbs = new Map();
  const shardByConversation = new Map();
  let error = '';
  let sqlite;
  try { sqlite = Process.getModuleByName('sqlite3.dll'); }
  catch (_) { return {read() { return {ok:false,error:'sqlite_unavailable',rows:[]}; }, status() {return {databases:0,error:'sqlite_unavailable'};}}; }
  function fn(name, result, args) { return new NativeFunction(sqlite.getExportByName(name), result, args); }
  const filename = fn('sqlite3_db_filename','pointer',['pointer','pointer']);
  const dbHandle = fn('sqlite3_db_handle','pointer',['pointer']);
  const main = Memory.allocUtf8String('main');
  const prepare = fn('sqlite3_prepare_v2','int',['pointer','pointer','int','pointer','pointer']);
  const step = fn('sqlite3_step','int',['pointer']);
  const finalize = fn('sqlite3_finalize','int',['pointer']);
  const columnCount = fn('sqlite3_column_count','int',['pointer']);
  const columnText = fn('sqlite3_column_text','pointer',['pointer','int']);
  const columnBytes = fn('sqlite3_column_bytes','int',['pointer','int']);
  const bindText = fn('sqlite3_bind_text','int',['pointer','int','pointer','int','pointer']);
  const stmtReadonly = fn('sqlite3_stmt_readonly','int',['pointer']);
  const kernel = Process.getModuleByName('kernel32.dll');
  const enter = new NativeFunction(kernel.getExportByName('AcquireSRWLockExclusive'),'void',['pointer']);
  const leave = new NativeFunction(kernel.getExportByName('ReleaseSRWLockExclusive'),'void',['pointer']);
  let discoveryHook = null;
  function observe(db) {
    const key=db.toString();
    if (dbs.has(key)) return true;
    try {
      const path=filename(db,main).readUtf8String();
      const match=path && /[\\/]MessageSDK[\\/]libaim[\\/](\d+)@cntaobao[\\/]database[\\/]im\.sqlite$/i.exec(path);
      if (!match || dbs.size>=16) return false;
      const gate=Memory.alloc(Process.pointerSize);gate.writePointer(ptr(0));
      dbs.set(key,{db:db,path:path,uid:match[1],tables:null,gate:gate});
      if (discoveryHook) {discoveryHook.detach();discoveryHook=null;}
      return true;
    } catch (_) {return false;}
  }
  // The initial statements may have been cached before injection. Stop this
  // temporary step hook once a message connection is found; keep prepare/close
  // hooks for login changes and new connections.
  function discover() {
    if (discoveryHook) return;
    discoveryHook=Interceptor.attach(sqlite.getExportByName('sqlite3_step'),{
      onEnter(args) {try {observe(dbHandle(args[0]));}catch(_) {}}
    });
  }
  for (const name of ['sqlite3_prepare_v2','sqlite3_prepare16_v2']) {
    Interceptor.attach(sqlite.getExportByName(name),{onEnter(args){observe(args[0]);}});
  }
  for (const name of ['sqlite3_close','sqlite3_close_v2']) {
    const address=sqlite.findExportByName(name);
    if (address) Interceptor.attach(address,{
      onEnter(args){
        const key=args[0].toString();this.item=dbs.get(key);
        if(this.item)enter(this.item.gate);
        dbs.delete(key);shardByConversation.clear();
      },
      onLeave(){if(this.item)leave(this.item.gate);}
    });
  }
  discover();
  function query(item,sql,values,limit) {
    // All statements and table names are constructed below. Values are bound.
    const out=Memory.alloc(Process.pointerSize);out.writePointer(ptr(0));
    const rc=prepare(item.db,Memory.allocUtf8String(sql),-1,out,ptr(0));
    if(rc!==0) throw new Error('sqlite_prepare_'+rc);
    const stmt=out.readPointer();
    const keep=[];
    try {
      if(stmtReadonly(stmt)!==1)throw new Error('read_only_required');
      for(let i=0;i<values.length;i++) {
        const text=Memory.allocUtf8String(String(values[i]));keep.push(text);
        if(bindText(stmt,i+1,text,-1,ptr(0))!==0)throw new Error('sqlite_bind_failed');
      }
      const rows=[];let state;
      while(rows.length<limit && (state=step(stmt))===100) {
        const row=[];
        for(let i=0;i<columnCount(stmt);i++) {
          if(columnBytes(stmt,i)>524288)throw new Error('message_too_large');
          const value=columnText(stmt,i);row.push(value.isNull()?null:value.readUtf8String());
        }
        rows.push(row);
      }
      if(state!==100 && state!==101)throw new Error('sqlite_step_'+state);
      return rows;
    } finally {finalize(stmt);}
  }
  return {
    status() {return {databases:dbs.size,error:error};},
    read(account,ccode,beforeMs,afterMs,messageId,count) {
      if(!/^\d+(?:\.\d+)?-\d+(?:\.\d+)?#\d+@cntaobao$/.test(String(ccode))) throw new Error('invalid_ccode');
      const owners=accountDetails().filter(x=>x.account===account && /^\d+$/.test(x.seller_id));
      const uids=new Set(owners.map(x=>x.seller_id));
      if(!uids.size)return {ok:false,error:'account_not_bound',rows:[]};
      count=Math.max(1,Math.min(100,Number(count)||100));
      beforeMs=Math.floor(Number(beforeMs));afterMs=Math.floor(Number(afterMs));
      if(!Number.isSafeInteger(beforeMs)||!Number.isSafeInteger(afterMs)||beforeMs<afterMs||afterMs<0)throw new Error('invalid_time_range');
      const rows=[];
      let available=false;
      for(const [key,item] of dbs) {
        if(!uids.has(item.uid))continue;
        enter(item.gate);
        try {
          if(dbs.get(key)!==item)continue;
          if(filename(item.db,main).readUtf8String()!==item.path){dbs.delete(key);continue;}
          available=true;
          if(!item.tables)item.tables=query(item,"SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'msg_[0-9][0-9][0-9]'",[],256).map(x=>x[0]);
          const cacheKey=key+'|'+ccode;
          const known=shardByConversation.get(cacheKey);
          const tables=known?[known]:item.tables;
          for(const table of tables) {
            if(!/^msg_\d{3}$/.test(table))continue;
            const values=[ccode,afterMs,beforeMs];
            let sql='SELECT cid,mid,sender_id,created_at,content_type,content_label,hex(content),extension,recall_status,delete_status FROM "'+table+'" WHERE cid=? AND created_at>=? AND created_at<=? AND recall_status=0 AND delete_status=0 AND length(content)<=262144';
            if(messageId){sql+=' AND mid=?';values.push(String(messageId));}
            sql+=' ORDER BY created_at DESC,_id DESC LIMIT '+count;
            const found=query(item,sql,values,count);
            if(found.length){rows.push(...found);shardByConversation.set(cacheKey,table);break;}
          }
        } catch(e) {error=String(e.message||e);return {ok:false,error:error,rows:[]};}
        finally {leave(item.gate);}
      }
      if(!available){discover();return {ok:false,error:'message_database_not_observed',rows:[]};}
      error='';
      rows.sort((a,b)=>Number(a[3])-Number(b[3]));
      return {ok:true,rows:rows.slice(-count)};
    }
  };
}
