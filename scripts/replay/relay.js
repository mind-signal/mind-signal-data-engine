#!/usr/bin/env node
// OPS-W019 T7 프록시와 백엔드 사이 지연 중계기임.
//
// 프록시의 BACKEND_URL을 이 중계기로 돌리면, 프록시의 /proxy 네임스페이스 연결을 받아
// 같은 auth(engineSecret)로 실제 백엔드 /proxy에 연결하고 이벤트를 ack까지 그대로 넘김.
// --delay-subject와 --delay-ms를 주면 그 subject의 proxy:sample만 늦게 넘김.
// --dup-every N을 주면 proxy:sample 프레임 N개마다 같은 프레임을 한 번 더 넘김
// (프록시 재연결 뒤 replay로 같은 envelope이 다시 오는 경우 재현).
// 프록시 수신 시각은 그대로이고 백엔드 도착만 늦어지는 경우(EEG-W006)를 재현함.
// /socket.io 밖의 HTTP 요청(프록시 be-notifier의 register-pending 등)은 그대로 전달함.
//
// 새 의존성 없이 백엔드 저장소의 socket.io를 씀:
//   NODE_PATH={backend}/node_modules node relay.js --port 5101 \
//     --backend-url http://127.0.0.1:5100 --delay-subject 2 --delay-ms 300
'use strict';

const http = require('http');
const { parseArgs } = require('util');

const USAGE = `사용법: NODE_PATH={backend}/node_modules node relay.js --port N --backend-url URL
       [--delay-subject 1|2 --delay-ms N] [--dup-every N]`;

/**
 * 인자를 해석함. 잘못되면 사용법을 출력하고 끝냄.
 * @returns {{port:number, backendUrl:string, delaySubject:number|null, delayMs:number,
 *   dupEvery:number}}
 */
function parseCli() {
  const { values } = parseArgs({
    options: {
      port: { type: 'string', default: '5101' },
      'backend-url': { type: 'string', default: 'http://127.0.0.1:5100' },
      'delay-subject': { type: 'string' },
      'delay-ms': { type: 'string', default: '0' },
      'dup-every': { type: 'string', default: '0' },
      help: { type: 'boolean', short: 'h' },
    },
  });
  if (values.help) {
    console.log(USAGE);
    process.exit(0);
  }
  const delaySubject = values['delay-subject'] ? Number(values['delay-subject']) : null;
  const delayMs = Number(values['delay-ms']);
  const dupEvery = Number(values['dup-every']);
  if (![null, 1, 2].includes(delaySubject) || !(delayMs >= 0) || !Number.isInteger(dupEvery) || dupEvery < 0) {
    console.error(USAGE);
    process.exit(2);
  }
  return {
    port: Number(values.port),
    backendUrl: values['backend-url'],
    delaySubject,
    delayMs,
    dupEvery,
  };
}

/**
 * 한쪽 소켓의 모든 이벤트를 다른 쪽으로 넘김. ack 콜백이 있으면 상대의 응답을 되돌림.
 * 상대가 응답하지 않으면 ack도 부르지 않아 프록시의 ack 시간 제한이 그대로 작동함.
 * @param {object} from 이벤트를 받는 소켓
 * @param {object} to 이벤트를 보낼 소켓
 * @param {(event:string, args:unknown[]) => number} delayOf 이벤트별 지연(ms)
 * @param {(event:string, args:unknown[]) => boolean} [dupOf] 참이면 같은 프레임을 한 번 더
 *   보냄. 중복분의 응답은 버림(프록시는 원래 프레임 하나의 ack만 기다림)
 */
function pipeEvents(from, to, delayOf, dupOf = () => false) {
  from.onAny((event, ...args) => {
    const ack = typeof args[args.length - 1] === 'function' ? args.pop() : null;
    const dup = dupOf(event, args);
    const send = () => {
      if (ack) to.emit(event, ...args, (...res) => ack(...res));
      else to.emit(event, ...args);
      if (dup) to.emit(event, ...args, () => {});
    };
    const ms = delayOf(event, args);
    if (ms > 0) setTimeout(send, ms);
    else send();
  });
}

function main() {
  const { Server } = require('socket.io');
  const { io: connect } = require('socket.io-client');
  const cfg = parseCli();
  const backend = new URL(cfg.backendUrl);

  // socket.io가 처리하지 않는 HTTP 요청은 백엔드로 그대로 넘김
  const server = http.createServer((req, res) => {
    const upstream = http.request(
      {
        hostname: backend.hostname,
        port: backend.port,
        path: req.url,
        method: req.method,
        headers: { ...req.headers, host: backend.host },
      },
      (up) => {
        res.writeHead(up.statusCode, up.headers);
        up.pipe(res);
      },
    );
    upstream.on('error', (err) => {
      res.writeHead(502, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'relay_upstream_failed', detail: err.message }));
    });
    req.pipe(upstream);
  });

  const nsp = new Server(server, { cors: { origin: '*' } }).of('/proxy');
  const counts = { forwarded: 0, delayed: 0, duplicated: 0 };
  let samples = 0;

  // 백엔드 연결이 성공해야 프록시 연결을 받음. 시크릿 오류도 프록시에 그대로 전달됨
  nsp.use((socket, next) => {
    const up = connect(`${cfg.backendUrl}/proxy`, {
      transports: ['websocket'],
      auth: socket.handshake.auth,
      reconnection: false,
    });
    // 미들웨어 처리 중에 프록시가 끊기면 connection이 오지 않으므로 여기서도 업스트림을 닫음
    socket.conn.once('close', () => up.close());
    up.once('connect', () => {
      socket.data.up = up;
      next();
    });
    up.once('connect_error', (err) => {
      up.close();
      next(err);
    });
  });

  nsp.on('connection', (socket) => {
    const up = socket.data.up;
    console.log(`[relay] proxy 연결됨: ${socket.id}`);
    pipeEvents(
      socket,
      up,
      (event, args) => {
        counts.forwarded++;
        const subject = args[0] && args[0].subject_idx;
        if (event === 'proxy:sample' && subject === cfg.delaySubject && cfg.delayMs > 0) {
          counts.delayed++;
          return cfg.delayMs;
        }
        return 0;
      },
      (event) => {
        if (event !== 'proxy:sample' || cfg.dupEvery === 0) return false;
        samples++;
        if (samples % cfg.dupEvery !== 0) return false;
        counts.duplicated++;
        return true;
      },
    );
    pipeEvents(up, socket, () => 0);
    socket.on('disconnect', () => up.close());
    up.on('disconnect', () => socket.disconnect(true));
  });

  // 누적 요약을 값이 바뀐 때만 1초 안에 찍음. 30초 주기면 드라이버가 측정 직후
  // relay.log를 읽을 때 마지막 구간의 중복 수가 빠짐(OPS-W019 7절 B6)
  let last = '';
  setInterval(() => {
    const line = `[relay] 전달 ${counts.forwarded}, 지연 ${counts.delayed}, 중복 ${counts.duplicated}`;
    if (line !== last) console.log((last = line));
  }, 1000).unref();

  server.listen(cfg.port, '127.0.0.1', () => {
    const delay = cfg.delaySubject ? `subject ${cfg.delaySubject} +${cfg.delayMs}ms` : '지연 없음';
    const dup = cfg.dupEvery ? `, ${cfg.dupEvery}개마다 중복` : '';
    console.log(`[relay] :${cfg.port} → ${cfg.backendUrl} (${delay}${dup})`);
  });
}

if (require.main === module) main();

module.exports = { pipeEvents, parseCli };
