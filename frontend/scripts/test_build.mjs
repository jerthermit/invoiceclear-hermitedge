import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { cp, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const temporary = await mkdtemp(join(tmpdir(),'invoiceclear-build-'));
const sourceInteractions = await readFile(new URL('../web/interactions.js',import.meta.url),'utf8');
const root = join(temporary,'frontend');
const env = {...process.env};
delete env.VERCEL; delete env.INVOICECLEAR_API_BASE_URL;
let passed = 0;
try {
  await mkdir(join(root,'scripts'),{recursive:true});
  await mkdir(join(root,'web'),{recursive:true});
  await cp(new URL('./build_frontend.mjs',import.meta.url),join(root,'scripts/build_frontend.mjs'));
  await writeFile(join(root,'web/index.html'),'<!doctype html><html><head><title>Retained UI</title></head><body></body></html>');
  await writeFile(join(root,'web/config.js'),'window.__INVOICECLEAR_CONFIG__ = {};');
  await cp(new URL('../web/interactions.js',import.meta.url),join(root,'web/interactions.js'));
  await cp(new URL('../web/art/',import.meta.url),join(root,'web/art'),{recursive:true});
  const build = (variables = {}, flags = []) => spawnSync(process.execPath,[join(root,'scripts/build_frontend.mjs'),...flags],{env:{...env,...variables},encoding:'utf8'});
  const check = (name,task) => { task(); passed++; process.stdout.write(`PASS ${name}\n`); };
  const local = build();
  const localConfig = await readFile(join(root,'dist/config.js'),'utf8');
  const localHtml = await readFile(join(root,'dist/index.html'),'utf8');
  check('local build supports the same-origin backend without changing source HTML',()=>{
    assert.equal(local.status,0,local.stderr); assert.match(localConfig,/"apiBaseUrl":""/);
    assert.match(localHtml,/content="connect-src 'self'"/);
  });
  const shippedInteractions=await readFile(join(root,'dist/interactions.js'),'utf8');
  const actualIndex=await readFile(new URL('../web/index.html',import.meta.url),'utf8');
  check('the local interaction layer ships unchanged and loads before application hooks',()=>{
    assert.equal(shippedInteractions,sourceInteractions);
    assert.ok(actualIndex.indexOf('src="/interactions.js"')<actualIndex.indexOf('src="/app.js"'));
    assert.match(actualIndex,/<script src="\/interactions\.js" defer>/);
  });
  for(const asset of ['handoff-folio.svg','paper-grain.svg']) {
    const original=await readFile(new URL(`../web/art/${asset}`,import.meta.url),'utf8');
    const shipped=await readFile(join(root,'dist/art',asset),'utf8');
    check(`original ${asset} ships locally without executable or remote content`,()=>{
      assert.equal(shipped,original);assert.match(shipped,/<svg /);
      assert.doesNotMatch(shipped,/<script|<foreignObject|\bon\w+=|(?:href|src)=["'](?:https?:|\/\/)|<text\b/);
    });
  }
  const deployed = build({INVOICECLEAR_API_BASE_URL:'https://api.example.test/',TOGETHER_API_KEY:'never-ship-this-provider-key'},['--deploy']);
  const config = await readFile(join(root,'dist/config.js'),'utf8');
  const html = await readFile(join(root,'dist/index.html'),'utf8');
  check('deployed build pins browser connections to the configured HTTPS origin only',()=>{
    assert.equal(deployed.status,0,deployed.stderr); assert.match(config,/"apiBaseUrl":"https:\/\/api.example.test"/);
    assert.match(html,/content="connect-src 'self' https:\/\/api.example.test"/);
    assert.doesNotMatch(config+html,/never-ship-this-provider-key/);
    assert.ok(html.indexOf('http-equiv="Content-Security-Policy"')<html.indexOf('<title>'));
  });
  const hosting = JSON.parse(await readFile(new URL('../vercel.json',import.meta.url),'utf8'));
  check('Vercel explicitly enables deployment validation regardless of system environment exposure',()=>{
    assert.equal(hosting.buildCommand,'node scripts/build_frontend.mjs --deploy');
    assert.equal(hosting.outputDirectory,'dist');
    assert.equal(hosting.framework,null); assert.equal(hosting.installCommand,'npm ci --ignore-scripts');
  });
  const manifest = JSON.parse(await readFile(new URL('../package.json',import.meta.url),'utf8'));
  const lock = JSON.parse(await readFile(new URL('../package-lock.json',import.meta.url),'utf8'));
  const runtime = await readFile(new URL('../.nvmrc',import.meta.url),'utf8');
  check('frontend runtime and dependency-free lockfile match its deployment scripts',()=>{
    assert.equal(manifest.engines.node,'22.x'); assert.equal(runtime.trim(),'22');
    assert.equal(manifest.private,true); assert.equal(lock.name,manifest.name);
    assert.equal(lock.packages[''].engines.node,'22.x');
    assert.equal(manifest.scripts.build,'node scripts/build_frontend.mjs --deploy');
    assert.equal(manifest.scripts['build:local'],'node scripts/build_frontend.mjs');
    assert.deepEqual(Object.keys(manifest.dependencies || {}),[]);
    assert.deepEqual(Object.keys(lock.packages),['']);
  });
  check('deployment cannot silently build without its backend',()=>{
    for (const [variables,flags] of [[{},['--deploy']],[{VERCEL:'1'},[]]]) {
      const result = build(variables,flags); assert.notEqual(result.status,0); assert.match(result.stderr,/Set INVOICECLEAR_API_BASE_URL/);
    }
  });
  check('deployed builds reject HTTP and loopback backend addresses',()=>{
    for (const origin of ['http://api.example.test','https://localhost','https://127.0.0.1:8100','https://[::1]:8100','https://shop.localhost']) {
      assert.notEqual(build({INVOICECLEAR_API_BASE_URL:origin},['--deploy']).status,0,origin);
    }
  });
  check('invalid origins fail without logging credentials or accepting wildcard policy',()=>{
    for (const origin of ['not-a-url','https://api.example.test/api','https://api.example.test?key=never-log-this','https://api.example.test#fragment','https://user:never-log-this@api.example.test','https://*.example.test','https://api.\nexample.test',"https://api'.example.test"]) {
      const result=build({INVOICECLEAR_API_BASE_URL:origin});
      assert.notEqual(result.status,0); assert.doesNotMatch(result.stderr,/never-log-this/);
    }
  });
  check('local development can still target a loopback HTTP backend',()=>{
    const result=build({INVOICECLEAR_API_BASE_URL:'http://127.0.0.1:8100'}); assert.equal(result.status,0,result.stderr);
  });
  const before = await readFile(join(root,'dist/config.js'),'utf8');
  const rejected=build({},['--deploy']);
  check('invalid deployment validation leaves a previously generated build untouched',()=>{assert.notEqual(rejected.status,0);});
  assert.equal(await readFile(join(root,'dist/config.js'),'utf8'),before);
  assert.equal(await readFile(join(root,'web/index.html'),'utf8'),'<!doctype html><html><head><title>Retained UI</title></head><body></body></html>');
  process.stdout.write(`${passed} frontend build regression groups passed.\n`);
} finally {
  await rm(temporary,{recursive:true,force:true});
}
