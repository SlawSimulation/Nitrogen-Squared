// GPL-3.0-or-later. Development-only synthetic adapter; Vite removes it from releases.
const handlers=new Map();let callbackId=0;const callbacks=new Map();
const state={revision:1,version:'0.2.2',platform:'Windows x64',busy:false,phase:'idle',title:'Choose your game disc',detail:'Your game, settings and saves stay in this folder.',percent:null,error:null,recovery:false,disc:null,prepared:false,imported:false,extracted:false,accent:'Cyan',options:{video_mode:0,hud_scale:0,render_percent:100,master_volume:100},keyboard:[['Move / navigate','W A S D'],['Select / main weapon','Enter'],['Back / shield','Backspace'],['Jump','Space']],mods_revision:0,releases:[{version:'0.2.2',title:'Nitrogen Squared 0.2.2',date:'2026-11-09',summary:'A smoother home for your next run.',body:'A Tauri launcher adapted from Recompia.\n\nYour game and saves stay local.',current:true}],update:null};
const synthetic=(id,name,plugins,state,detail)=>({id,version:'1.0.0',name,author:'Synthetic author',license:'GPL-3.0-or-later',description:`Synthetic ${name} package for interface checks.`,required_plugins:plugins,missing_plugins:state==='missing-plugins'?plugins:[],enabled:false,compatibility:{state,detail}});
const mods={packages:[synthetic('example.ready','Ready Example',['nitrogen.mod-probe'],'ready','Compatible with the current game build'),synthetic('example.missing','Missing Plugin Example',['example.absent'],'missing-plugins','This game build lacks example.absent')],checked:true,problem:null};
function emit(event,payload){for(const id of handlers.get(event)||[])callbacks.get(id)?.({event,payload});}
window.__N2_MOCK__={state,emit,mods};
window.__TAURI_INTERNALS__={
  transformCallback(fn,once=false){const id=++callbackId;callbacks.set(id,once?(...args)=>{callbacks.delete(id);fn(...args);}:fn);return id;},
  unregisterCallback(id){callbacks.delete(id);},
  metadata:{currentWindow:{label:'main'},currentWebview:{label:'main'}},
  async invoke(cmd,args={}){
    if(cmd==='plugin:event|listen'){const ids=handlers.get(args.event)||[];ids.push(args.handler);handlers.set(args.event,ids);return args.handler;}
    if(cmd==='plugin:event|unlisten')return;
    if(cmd==='plugin:window|is_maximized')return false;
    if(cmd.startsWith('plugin:window|'))return;
    if(cmd==='host_info')return {testing:false};
    if(cmd==='choose_disc'){state.disc='C:\\Games\\N2O.cue';state.imported=true;state.revision++;emit('state',{...state});return structuredClone(state);}
    if(cmd==='lifecycle')return;
    if(cmd==='choose_mod'){mods.packages.push(synthetic('example.installed','Installed Example',[],'ready','Compatible with the current game build'));state.mods_revision++;state.revision++;emit('state',structuredClone(state));return structuredClone(state);}
    if(cmd==='request'&&args.command==='mods')return structuredClone({...mods,revision:state.mods_revision});
    if(cmd==='request'){
      if(args.command==='save_option')state.options[args.args.key]=args.args.value;
      if(args.command==='accent')state.accent=args.args.value;
      if(args.command==='play'){state.busy=true;state.phase='build';state.title='Building game';state.detail='42 of 100 · Building CXX object native/movie_player.cpp';state.workers='12 of 16 logical CPUs';state.elapsed=65;state.percent=42;}
      if(args.command==='stop'){state.busy=false;state.phase='idle';state.percent=null;state.title='Setup stopped';}
      if(args.command==='mod_enable'){const p=mods.packages.find(p=>p.id===args.args.id);if(args.args.enabled&&p.compatibility.state!=='ready')throw `${p.name} cannot be enabled: ${p.compatibility.detail}`;p.enabled=args.args.enabled;state.mods_revision++;}
      if(args.command==='mod_remove'){mods.packages=mods.packages.filter(p=>p.id!==args.args.id);state.mods_revision++;}
      if(args.command==='check_update')state.title='You have the latest release.';
      state.revision++;emit('state',structuredClone(state));return structuredClone(state);
    }
    throw Error('Unsupported mock '+cmd);
  }
};
