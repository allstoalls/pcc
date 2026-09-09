// Independent HTTP/2 implementation: Node built-ins, temporary local TLS origin.
import net from 'node:net';
import tls from 'node:tls';
import http2 from 'node:http2';
import {readFileSync} from 'node:fs';
import {once} from 'node:events';
import {createHash} from 'node:crypto';

const [proxyPort,mode,scenario,certPath,keyPath,durationArg] = process.argv.slice(2);
const longDuration=Number(durationArg||245000);
if(!Number.isFinite(longDuration)||longDuration<100||longDuration>3600000)throw new Error('Invalid duration');
const cert=readFileSync(certPath),key=readFileSync(keyPath);
const hash=data=>createHash('sha256').update(data).digest('hex');
const body=index=>Buffer.alloc(128*1024,index+1);
const origin=http2.createSecureServer({cert,key});
const originSessions=new Set();
const timers=new Set();
const observedEvents=[];
origin.on('session',s=>{originSessions.add(s);s.on('error',()=>{});s.on('close',()=>originSessions.delete(s));});
origin.on('stream',(stream,headers)=>{
  const index=Number(headers[':path'].slice(1));
  stream.on('error',()=>{});
  stream.respond({':status':200});
  if(scenario==='reset' && index===0){
    // Reset an established response; closing before headers flush can become EOF.
    stream.write(Buffer.from('partial-body'));
    const timer=setTimeout(()=>{
      timers.delete(timer);
      if(!stream.destroyed)stream.destroy(new Error('Injected HTTP2 stream failure'));
    },25);
    timers.add(timer);
  }else if(scenario==='long_idle'){
    stream.write(Buffer.alloc(4096,index+1));
    const timer=setTimeout(()=>{
      timers.delete(timer);
      if(!stream.destroyed)stream.end(Buffer.alloc(4096,index+1));
    },longDuration);
    timers.add(timer);
  }else if(scenario==='stream' || scenario==='long_stream'){
    let n=0;
    const timer=setInterval(()=>{
      if(stream.destroyed){clearInterval(timer);timers.delete(timer);return;}
      stream.write(Buffer.alloc(1024,index+1));
      if(++n===8){clearInterval(timer);timers.delete(timer);stream.end();}
    },scenario==='long_stream'?longDuration/8:25);
    timers.add(timer);
  }else{
    stream.end(body(index));
    if(scenario==='goaway' && index===7)stream.session.goaway(0,stream.id);
  }
});

function readExactly(socket,n){
  return new Promise((resolve,reject)=>{
    const clean=()=>{socket.off('readable',ready);socket.off('end',end);socket.off('error',fail);};
    const fail=e=>{clean();reject(e);};
    const end=()=>fail(new Error('Unexpected handshake EOF'));
    const ready=()=>{const b=socket.read(n);if(b){clean();resolve(b);}};
    socket.on('readable',ready);socket.once('end',end);socket.once('error',fail);ready();
  });
}

let client,raw,secure;
const deadline=setTimeout(()=>{console.error('Fixture deadline exceeded');process.exit(2);},scenario.startsWith('long_')?longDuration+10000:10000);
try{
  origin.listen(0,'127.0.0.1');await once(origin,'listening');
  const port=origin.address().port;
  raw=net.connect({host:'127.0.0.1',port:mode==='direct'?port:Number(proxyPort)});await once(raw,'connect');
  if(mode==='socks5'){
    raw.write(Buffer.from([5,1,0]));
    if(!(await readExactly(raw,2)).equals(Buffer.from([5,0])))throw new Error('SOCKS negotiation failed');
    raw.write(Buffer.from([5,1,0,1,127,0,0,1,port>>8,port&255]));
    const reply=await readExactly(raw,4);
    if(reply[1]!==0)throw new Error(`SOCKS rejected: ${reply[1]}`);
    const size=reply[3]===1?4:reply[3]===4?16:(await readExactly(raw,1))[0];
    await readExactly(raw,size+2);
  }else if(mode==='http'){
    raw.write(`CONNECT 127.0.0.1:${port} HTTP/1.1\r\nHost: 127.0.0.1:${port}\r\n\r\n`);
    let response='';
    while(!response.endsWith('\r\n\r\n')){
      response+=(await readExactly(raw,1)).toString();
      if(response.length>8192)throw new Error('CONNECT header size exceeded');
    }
    if(response.split(' ')[1]!=='200')throw new Error('CONNECT rejected');
  }
  secure=tls.connect({socket:raw,servername:'localhost',ca:cert,rejectUnauthorized:true,ALPNProtocols:['h2']});
  await once(secure,'secureConnect');
  if(secure.alpnProtocol!=='h2')throw new Error('HTTP/2 ALPN negotiation failed');
  client=http2.connect('https://localhost',{createConnection:()=>secure});
  const sessionErrors=[],goaway=[];
  client.on('error',e=>sessionErrors.push(e.code));
  client.on('goaway',(code,lastStreamID)=>goaway.push({code,lastStreamID}));
  await once(client,'connect');
  const results=await Promise.all(Array.from({length:8},(_,index)=>new Promise(resolve=>{
    const request=client.request({':path':`/${index}`});
    const chunks=[];let status,done=false,lastData=Date.now(),maxDataGapMs=0;
    const start=Date.now();
    const finish=error=>{
      if(done)return;done=true;
      const expected=scenario==='stream'||scenario.startsWith('long_')?Buffer.alloc(8192,index+1):body(index);
      resolve({index,status,bytes:chunks.reduce((n,b)=>n+b.length,0),
        correctBody:hash(Buffer.concat(chunks))===hash(expected),error:error?.code||null,rstCode:request.rstCode,
        durationMs:Date.now()-start,maxDataGapMs});
    };
    request.on('response',h=>status=h[':status']);
    request.on('data',b=>{maxDataGapMs=Math.max(maxDataGapMs,Date.now()-lastData);lastData=Date.now();chunks.push(b);});
    request.once('end',()=>{observedEvents.push({index,event:'end',rstCode:request.rstCode});finish();});
    request.once('error',e=>{observedEvents.push({index,event:'error',code:e.code,rstCode:request.rstCode});finish(e);});
    request.once('close',()=>{observedEvents.push({index,event:'close',rstCode:request.rstCode});if(!done)finish({code:'PREMATURE_CLOSE'});});
    request.end();
  })));
  console.log(JSON.stringify({alpn:secure.alpnProtocol,authorized:secure.authorized,mode,scenario,results,goaway,sessionErrors,observedEvents}));
}finally{
  client?.destroy();secure?.destroy();raw?.destroy();
  for(const timer of timers)clearInterval(timer);
  for(const session of originSessions)session.destroy();
  await new Promise(resolve=>origin.close(resolve));
  clearTimeout(deadline);
}
