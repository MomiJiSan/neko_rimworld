import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
const repo = fileURLToPath(new URL("../../../../", import.meta.url));
const plugin = fileURLToPath(new URL("../", import.meta.url));
const front = path.join(repo, "frontend/plugin-manager");
const { Window } = await import(pathToFileURL(path.join(front, "node_modules/happy-dom/lib/index.js")).href);
const { transform } = await import(pathToFileURL(path.join(front, "node_modules/sucrase/dist/index.js")).href);
const { bundleHostedTsxSource } = await import(pathToFileURL(path.join(front, "src/components/plugin/hosted/hostedTsxModule.mjs")).href);
const runtime = await fs.readFile(path.join(front, "src/components/plugin/hosted/ui-kit/runtime.js"), "utf8");
const source = await fs.readFile(path.join(plugin, "ui/panel.tsx"), "utf8");
const dependencies = [];
async function collect(dir) {
  for (const entry of await fs.readdir(dir, { withFileTypes: true })) {
    const absolute = path.join(dir, entry.name);
    if (entry.isDirectory()) await collect(absolute);
    else if (/\.tsx?$/.test(entry.name) && entry.name !== "panel.tsx") dependencies.push({ path: path.relative(plugin, absolute).replaceAll("\\", "/"), source: await fs.readFile(absolute, "utf8") });
  }
}
await collect(path.join(plugin, "ui"));
const compiled = transform(bundleHostedTsxSource(source, dependencies, "ui/panel.tsx"), { transforms:["typescript","jsx"], jsxPragma:"h", jsxFragmentPragma:"Fragment", production:true }).code.replace(/export\s+default\s+function\s+([\w$]*)\s*\(/, "const __Panel = function $1(");
const flush = async () => { for (let i=0;i<8;i++) await Promise.resolve(); await new Promise(resolve => setTimeout(resolve, 1)); };
function setup() {
  const window = new Window({ url:"http://localhost/ui" });
  window.__NEKO_PAYLOAD={ locale:"zh-CN", host:{origin:"http://localhost"}, i18n:{messages:{}} };
  const NativeText = window.Text;
  window.eval(runtime);
  window.Text = NativeText; // happy-dom constructs text through window.Text; browsers use their native DOM implementation.
  window.eval(compiled+"\nwindow.TestPanel=__Panel;");
  const ui=window.NekoUiKit;
  const root=window.document.createElement("main");window.document.body.append(root);
  const state={ action_nonce:"test-nonce", status:"ready", port:48919, configuration:{token_configured:true,agent_configured:true,agent_api_key_configured:true,agent_base_url:"https://example.com/v1",agent_model:"test"}, agent:{active_count:0,recent_count:0,active_runs:[],recent_runs:[],latest_run:null} };
  const calls=[];
  let invoke=async (id,args) => ({ result:{ status:"ok" } });
  const props={ state, plugin:{id:"neko_rimworld",name:"RimWorld"}, actions:["rimworld_report","get_agent_run_details","rimworld_consult","probe_game_overview","save_agent_settings","save_bridge_settings"].map(id=>({id,entry_id:id})), warnings:[], locale:"zh-CN", api:{call:async(id,args)=>{calls.push({id,args});return invoke(id,args)}, refresh:async()=>render()} };
  function render(){ui.render(ui.h(window.TestPanel,props),root)}
  function button(label, selector="button") { const found=[...root.querySelectorAll(selector)].find(x=>x.textContent===label);assert.ok(found,`Missing ${label}: ${root.textContent}`);return found; }
  async function click(label,selector="button"){button(label,selector).click();await flush()}
  async function tab(label){await click(label,'[role="tab"]')}
  async function type(input,value){assert.ok(input);input.value=value;input.dispatchEvent(new window.Event("input",{bubbles:true}));await flush()}
  render();
  return {window,root,props,state,calls,render,button,click,tab,type,setInvoke(fn){invoke=fn},async close(){ui.render(null,root);await window.happyDOM.abort();window.close()}};
}

function summary(id,question){return {run_id:id,question,state:"completed",result_status:"ok",result_summary:question+" summary",event_count:61,model_calls:1,tool_calls:1,duration_ms:1000}}
function detail(id,question,offset=0){return {...summary(id,question),events:[{event_seq:offset+1,event:"model_response_received",elapsed_ms:100,data:{content:question}}],events_page:{offset,limit:50,total:61,has_more:offset===0},result:{status:"ok",answer:question+" full",summary:question+" summary",observations:[]}}}

test("four pages preserve drafts across navigation and server refresh",async()=>{
  const h=setup();try{
    await flush();assert.equal(h.root.querySelectorAll('input[type="password"]').length,0);
    await h.tab("设置");
    await h.type(h.root.querySelector('input[placeholder="https://example.com/v1"]'),"https://draft.example/v1");
    await h.type(h.root.querySelectorAll('input[type="password"]')[1],"temporary-test-secret");
    await h.tab("调试");await h.type(h.root.querySelector("textarea"),"unfinished question");
    await h.tab("主页");h.state.configuration.agent_base_url="https://server-change.example/v1";h.render();await flush();
    await h.tab("设置 · 未保存");
    assert.equal(h.root.querySelector('input[placeholder="https://example.com/v1"]').value,"https://draft.example/v1");
    assert.equal(h.root.querySelectorAll('input[type="password"]')[1].value,"temporary-test-secret");
    await h.tab("调试");assert.equal(h.root.querySelector("textarea").value,"unfinished question");
    assert.equal(h.calls.length,0);
  }finally{await h.close()}
});

test("running consultation survives navigation and prevents duplicate submit",async()=>{
  const h=setup();try{
    let finish;
    h.setInvoke(async(id,args)=>{
      if(id==="rimworld_consult")return await new Promise(resolve=>{finish=resolve});
      if(id==="get_agent_run_details")return {result:detail(args.run_id,"completed question",args.event_offset)};
      return {result:{status:"ok"}};
    });
    await h.tab("调试");const run=h.button("运行咨询");run.click();run.click();await flush();
    assert.equal(h.calls.filter(x=>x.id==="rimworld_consult").length,1);
    await h.tab("主页");assert.match(h.root.textContent,/Agent 运行中/);
    finish({result:{status:"degraded",summary:"A limited result",audit:{run_id:"run-new"}}});await flush();
    await h.tab("调试");assert.match(h.root.textContent,/A limited result/);
    await h.click("打开本次会话详情");assert.match(h.root.textContent,/completed question full/);
    assert.equal(h.root.querySelector('#rimworld-main-nav-tab-sessions').getAttribute("aria-selected"),"true");
    assert.equal(h.calls.filter(x=>x.id==="get_agent_run_details").length,1);
  }finally{await h.close()}
});

test("selected historical run, event pages and expiry never switch to latest",async()=>{
  const h=setup();try{
    const first=summary("r1","Older question");const second=summary("r2","Newer question");
    h.state.agent.recent_runs=[second,first];h.state.agent.latest_run=second;h.render();await flush();
    let expired=false;
    h.setInvoke(async(id,args)=>{
      if(expired)throw new Error("This run is no longer retained");
      return {result:detail(args.run_id,args.run_id==="r1"?"Older question":"Newer question",args.event_offset)};
    });
    await h.tab("会话与事件");
    const row=[...h.root.querySelectorAll("tr")].find(x=>x.textContent.includes("Older question"));assert.ok(row);row.click();await flush();
    assert.match(h.root.textContent,/Older question full/);
    h.state.agent.latest_run=summary("r3","Newest");h.render();await flush();assert.match(h.root.textContent,/Older question full/);
    await h.tab("事件");await h.click("下一页");assert.equal(h.calls.at(-1).args.event_offset,50);
    expired=true;h.state.agent.recent_runs=[h.state.agent.latest_run];h.render();await flush();
    await h.click("刷新详情");assert.match(h.root.textContent,/no longer retained/);
    assert.doesNotMatch(h.root.textContent,/Newest full/);
  }finally{await h.close()}
});

test("budget edits are included in save and secret inputs clear only after success",async()=>{
  const h=setup();try{
    await h.tab("设置");const numbers=h.root.querySelectorAll('input[type="number"]');await h.type(numbers[4],"40");
    await h.type(h.root.querySelectorAll('input[type="password"]')[1],"temporary-key");
    await h.click("保存模型与运行预算");
    const saved=h.calls.find(x=>x.id==="save_agent_settings");assert.equal(saved.args.budgets.request_timeout_seconds,40);
    assert.equal(saved.args.ui_nonce,"test-nonce");assert.equal(h.root.querySelectorAll('input[type="password"]')[1].value,"");
    assert.match(h.root.textContent,/已保存/);
  }finally{await h.close()}
});


test("late run detail replies cannot replace a newer selection",async()=>{
  const h=setup();try{
    const first=summary("r1","First");const second=summary("r2","Second");
    h.state.agent.recent_runs=[first,second];h.state.agent.latest_run=first;h.render();await flush();
    const pending={};
    h.setInvoke((id,args)=>new Promise(resolve=>{pending[args.run_id]=resolve}));
    await h.tab("会话与事件");
    const row=[...h.root.querySelectorAll("tr")].find(x=>x.textContent.includes("Second"));row.click();await flush();
    pending.r2({result:detail("r2","Second")});await flush();
    pending.r1({result:detail("r1","First")});await flush();
    assert.match(h.root.textContent,/Second full/);assert.doesNotMatch(h.root.textContent,/First full/);
  }finally{await h.close()}
});

test("top navigation supports keyboard selection",async()=>{
  const h=setup();try{
    const tab=h.root.querySelector("#rimworld-main-nav-tab-home");tab.focus();
    tab.dispatchEvent(new h.window.KeyboardEvent("keydown",{key:"End",bubbles:true}));await flush();
    assert.equal(h.root.querySelector("#rimworld-main-nav-tab-settings").getAttribute("aria-selected"),"true");
    assert.ok(h.root.querySelector('input[type="password"]'));
  }finally{await h.close()}
});


test("saved reports show full body, preserve draft status and explicitly attach followups",async()=>{
  const h=setup();try{
    const id="a".repeat(32), full="完整正文。".repeat(500)+"报告末尾保留";
    h.setInvoke(async(entry,args)=>{
      if(entry==="rimworld_report" && args.action==="list")return {result:{reports:[{report_id:id,state:"draft",question:"历史调查",created_at:"2026-09-05"}],total:1,next_offset:null}};
      if(entry==="rimworld_report")return {result:{report:{report_id:id,state:"draft",question:"历史调查",full_text:full,observation_count:2}}};
      return {result:{status:"ok",summary:"followup done"}};
    });
    await h.tab("会话与事件");await h.tab("完整报告");
    const row=[...h.root.querySelectorAll("tr")].find(x=>x.textContent.includes("历史调查"));assert.ok(row);row.click();await flush();
    assert.ok(h.root.textContent.includes(id));assert.match(h.root.textContent,/报告末尾保留/);assert.match(h.root.textContent,/不能当作正式交付/);
    await h.click("复制完整正文");assert.equal(await h.window.navigator.clipboard.readText(),full);
    await h.click("基于这份报告追问");await h.type(h.root.querySelector("textarea"),"解释原报告的细节");await h.click("运行咨询");
    assert.equal(h.calls.find(x=>x.id==="rimworld_consult").args.report_id,id);
    await h.click("取消报告引用");await h.click("运行咨询");
    assert.equal(h.calls.filter(x=>x.id==="rimworld_consult").at(-1).args.report_id,undefined);
  }finally{await h.close()}
});

test("late report reply cannot replace a newly selected report",async()=>{
  const h=setup();try{
    const first="a".repeat(32),second="b".repeat(32),pending={};
    h.setInvoke(async(entry,args)=>{
      if(args.action==="list")return {result:{reports:[{report_id:first,question:"报告一"},{report_id:second,question:"报告二"}],total:2}};
      return await new Promise(resolve=>{pending[args.report_id]=resolve});
    });
    await h.tab("会话与事件");await h.tab("完整报告");
    [...h.root.querySelectorAll("tr")].find(x=>x.textContent.includes("报告一")).click();await flush();
    [...h.root.querySelectorAll("tr")].find(x=>x.textContent.includes("报告二")).click();await flush();
    pending[second]({result:{report:{report_id:second,full_text:"第二份正文",state:"complete"}}});await flush();
    pending[first]({result:{report:{report_id:first,full_text:"第一份正文",state:"complete"}}});await flush();
    assert.match(h.root.textContent,/第二份正文/);assert.doesNotMatch(h.root.textContent,/第一份正文/);
  }finally{await h.close()}
});
