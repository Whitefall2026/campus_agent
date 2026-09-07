// 模拟 dsh-univer-office lib 中的旋转字符串表,解码宿主浏览器查找路径
import fs from 'node:fs';

const file = 'C:/Users/shurenzZ/.dsh/profiles/web/node_modules/dsh-univer-office/lib/index.js';
const src = fs.readFileSync(file, 'utf8');

// 取 _0x209f3c = [ ... ] 字面量 (行内可能很长)
const m = src.match(/const _0x209f3c = \[([\s\S]*?)\];\s*\n\s*_0x4995 = function/);
if (!m) { console.error('array literal not found'); process.exit(1); }
const body = m[1];
const items = [];
for (const q of body.matchAll(/"((?:[^"\\]|\\.)*)"/g)) {
  items.push(q[1].replace(/\\"/g, '"').replace(/\\\\/g, '\\'));
}
const N = items.length;
console.log('items:', N);

const idx = (k) => items[k - 272]; // _0x5c0c(k)
const value = (k) => { const v = idx(k); const n = parseInt(v, 10); return Number.isNaN(n) ? 0 : n; };

// 校验表达式:与 bundle 内 while 循环一致(求值顺序)
let found = -1;
for (let s = 0; s < N; s++) {
  const a = items;
  const calc =
    -value(347) / 1 + value(337) / 2 + value(301) / 3 * (-value(314) / 4) + value(357) / 5 * (value(381) / 6) +
    -value(455) / 7 * (value(452) / 8) + -value(351) / 9 + value(367) / 10 * (value(392) / 11);
  if (Math.abs(calc - 428125) < 1e-9) { found = s; break; }
  items.push(items.shift());
}
console.log('rotation shifts found:', found);
if (found < 0) { console.error('no rotation matched'); process.exit(1); }

const show = (k) => console.log(`[${k}] => ${JSON.stringify(items[k - 272])}`);
for (const k of [401, 428, 433, 309, 272, 356, 353, 416, 447, 331, 299, 372, 408, 403, 413, 335, 358, 364, 362, 390, 361, 311, 296, 439, 461, 375, 459, 387, 388, 410]) show(k);
