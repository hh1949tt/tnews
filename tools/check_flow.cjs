// 校验 flow 内所有 function 节点语法，并模拟运行「⚙️ 统一配置」节点看拼出的 URL
const fs = require('fs');
const path = require('path');

const file = path.join(__dirname, '..', 'extras', 'nodered-tnews-flow.json');
const nodes = JSON.parse(fs.readFileSync(file, 'utf8'));

let bad = 0;
for (const n of nodes) {
  if (n.type !== 'function' || !n.func) continue;
  try {
    new Function('msg', 'node', 'global', 'flow', 'context', n.func);
    console.log('OK   ', n.id, '-', n.name);
  } catch (e) {
    bad++;
    console.log('FAIL ', n.id, '-', n.name, ':', e.message);
  }
}

// 模拟配置节点
const cfgNode = nodes.find(n => n.id === 'tn_cfg');
const store = new Map();
const mock = {
  flow: { set: (k, v) => store.set('flow:' + k, v), get: k => store.get('flow:' + k) },
  global: { set: (k, v) => store.set('global:' + k, v), get: k => store.get('global:' + k) },
  node: { status: s => console.log('   status:', JSON.stringify(s)), warn: m => console.log('   warn:', m), error: m => console.log('   err:', m) },
};
const run = (topic, overrides) => {
  const src = overrides ? cfgNode.func.replace(/"token: ''"/, "token: 'abc123'") : cfgNode.func;
  const fn = new Function('msg', 'node', 'global', 'flow', 'context', src);
  const msg = { topic };
  const out = fn(msg, mock.node, mock.global, mock.flow, {});
  console.log('topic=' + topic, '=> urls:', (out || []).filter(Boolean).map(m => m.url));
  return out;
};
console.log('\n-- 默认（访客）--');
run('latest');
run('content');
console.log('\n-- 填了 token --');
const t = cfgNode.func.replace("token: ''", "token: 'abc/123'").replace("program_id: ''", "program_id: 'morning'");
const fn2 = new Function('msg', 'node', 'global', 'flow', 'context', t);
for (const topic of ['latest', 'content']) {
  const out = fn2({ topic }, mock.node, mock.global, mock.flow, {});
  console.log('topic=' + topic, '=> urls:', (out || []).filter(Boolean).map(m => m.url));
}

console.log('\n-- 填了 ext（外部调用令牌，优先于 token）--');
const e2 = cfgNode.func.replace("ext: ''", "ext: 'EXTTOK123'")
  .replace("token: ''", "token: 'SHOULD_NOT_APPEAR'")
  .replace("user: ''", "user: 'ha'").replace("pin: ''", "pin: '123456'")
  .replace("program_id: ''", "program_id: 'morning'");
const fn3 = new Function('msg', 'node', 'global', 'flow', 'context', e2);
for (const topic of ['latest', 'content']) {
  const out = fn3({ topic }, mock.node, mock.global, mock.flow, {});
  const urls = (out || []).filter(Boolean).map(m => m.url);
  console.log('topic=' + topic, '=> urls:', urls);
  if (!urls.every(u => u.includes('ext=EXTTOK123') && !u.includes('SHOULD_NOT_APPEAR')
      && !u.includes('user=ha'))) {
    console.error('FAIL: ext 未生效或串用了其他鉴权参数');
    process.exitCode = 1;
  }
}

console.log('\n语法失败节点数:', bad);
process.exit(bad ? 1 : 0);
