// relay.js 동작 확인 스크립트임. tests/test_replay_relay.py가 실행함.
// 가짜 백엔드(/proxy 네임스페이스와 HTTP)를 띄우고 relay.js를 사이에 둬서
// HTTP 통과, 시크릿 거부, ack 왕복, subject 지연, --dup-every 같은 프레임 중복을 확인함.
'use strict';
const http = require('http');
const assert = require('assert');
const { spawn } = require('child_process');
const { Server } = require('socket.io');
const { io } = require('socket.io-client');

const [relayPath, bePort, relayPort] = process.argv.slice(2);
const SECRET = 'relay-check';

const srv = http.createServer((req, res) => {
  let body = '';
  req.on('data', (c) => (body += c));
  req.on('end', () => {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ path: req.url, method: req.method, body }));
  });
});
const received = [];
const nsp = new Server(srv).of('/proxy');
nsp.use((s, next) =>
  s.handshake.auth.engineSecret === SECRET ? next() : next(new Error('invalid_engine_secret')),
);
nsp.on('connection', (s) =>
  s.on('proxy:sample', (env, ack) => {
    received.push(env);
    ack({ ok: true, seq: env.seq });
  }),
);

srv.listen(Number(bePort), '127.0.0.1', async () => {
  const relay = spawn(
    process.execPath,
    [relayPath, '--port', relayPort, '--backend-url', `http://127.0.0.1:${bePort}`,
      '--delay-subject', '2', '--delay-ms', '300', '--dup-every', '2'],
    { env: process.env, stdio: ['ignore', 'pipe', 'inherit'] },
  );
  let out = '';
  relay.stdout.on('data', (d) => (out += d));
  const base = `http://127.0.0.1:${relayPort}`;
  try {
    for (let i = 0; i < 50; i++) {
      try { await fetch(base + '/x'); break; } catch { await new Promise((r) => setTimeout(r, 100)); }
    }
    const r = await (await fetch(base + '/api/engine/register-pending', { method: 'POST', body: 'a' })).json();
    assert.deepStrictEqual(r, { path: '/api/engine/register-pending', method: 'POST', body: 'a' });

    const bad = io(base + '/proxy', { transports: ['websocket'], auth: { engineSecret: 'no' }, reconnection: false });
    assert.strictEqual(await new Promise((res) => bad.once('connect_error', (e) => res(e.message))), 'invalid_engine_secret');

    const c = io(base + '/proxy', { transports: ['websocket'], auth: { engineSecret: SECRET } });
    await new Promise((res) => c.once('connect', res));
    const send = async (subject, seq) => {
      const t = Date.now();
      const env = { group_id: 'g', subject_idx: subject, seq, de_ts_ns: String(1000 + seq) };
      const ack = await c.timeout(2000).emitWithAck('proxy:sample', env);
      assert.deepStrictEqual(ack, { ok: true, seq });
      return Date.now() - t;
    };
    const t1 = await send(1, 1);
    const t2 = await send(2, 2);
    await send(1, 3);
    await send(1, 4);
    await new Promise((r) => setTimeout(r, 300));
    assert(t1 < 150, `subject 1 rtt ${t1}`);
    assert(t2 >= 300, `subject 2 rtt ${t2}`);
    // 2개마다 같은 프레임을 한 번 더 넘기므로 4개를 보내면 6개가 도착함
    assert.deepStrictEqual(received.map((e) => e.seq), [1, 2, 2, 3, 4, 4]);
    assert.deepStrictEqual(received[1], received[2]);
    assert.deepStrictEqual(received[4], received[5]);
    // 드라이버가 측정 직후 읽으므로 요약이 30초 주기를 기다리지 않고 곧 갱신돼야 함
    for (let i = 0; i < 20 && !out.includes('중복 2'); i++) await new Promise((r) => setTimeout(r, 100));
    assert(out.includes('[relay] 전달 4, 지연 1, 중복 2'), `relay 요약 없음: ${out}`);
    console.log('RELAY_CHECK_OK');
    c.close();
  } catch (e) {
    console.error(e);
    process.exitCode = 1;
  } finally {
    relay.kill();
    srv.close();
    setTimeout(() => process.exit(), 50);
  }
});
