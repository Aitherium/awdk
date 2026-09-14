var Hi=Object.defineProperty;var G=(e,t)=>()=>(e&&(t=e(e=0)),t);var oe=(e,t)=>{for(var n in t)Hi(e,n,{get:t[n],enumerable:!0})};function Pt(e){let t=ea[e];if(!t)throw new Error(`bonsai-gguf: unsupported ggml type ${e} (not in TYPE_TRAITS)`);return t}function Hr(e,t){let{blockSize:n,typeSize:r}=Pt(e);if(t%n!==0)throw new Error(`bonsai-gguf: element count ${t} not a multiple of block size ${n} for ${Pt(e).name}`);return t/n*r}var ea,Ye,Bn,Qr,zr,yt=G(()=>{"use strict";ea={0:{blockSize:1,typeSize:4,name:"F32"},1:{blockSize:1,typeSize:2,name:"F16"},8:{blockSize:32,typeSize:34,name:"Q8_0"},41:{blockSize:128,typeSize:18,name:"Q1_0"},42:{blockSize:128,typeSize:34,name:"Q2_0"}},Ye=128,Bn=128,Qr=18,zr=34});var D,uo,_t=G(()=>{"use strict";D={MAP_READ:1,MAP_WRITE:2,COPY_SRC:4,COPY_DST:8,STORAGE:128,UNIFORM:64},uo={READ:1,WRITE:2}});function bo(e,t){return Math.floor((e+t-1)/t)}function Nn(e){Xe.has(e)||Xe.set(e,{enc:e.createCommandEncoder(),dispatches:0})}function Zt(e){let t=Xe.get(e);t&&(Xe.delete(e),e.queue.submit([t.enc.finish()]))}function Ve(e){let t=Xe.get(e);return t?{enc:t.enc,batched:!0}:{enc:e.createCommandEncoder(),batched:!1}}function Je(e,t){t.batched||e.queue.submit([t.enc.finish()])}function vo(e){let t=wo.get(e);return t||(t=new Map,wo.set(e,t)),t}function La(e){let t=yo.get(e);return t||(t={created:0,reused:0},yo.set(e,t)),t}function Rn(e,t){return`${e}:${t}`}function xo(e,t,n,r,o=!1){let i=vo(e),a=Rn(t,n),s=i.get(a),l=La(e);if(globalThis.__BONSAI_NO_POOL===!0)return l.created++,e.createBuffer({size:n,usage:t,label:r});if(s&&s.length>0){l.reused++;let d=s.pop();if(o)return d;let u=Ve(e);return u.enc.clearBuffer(d,0,n),Je(e,u),d}return l.created++,e.createBuffer({size:n,usage:t,label:r})}function Ot(e,t){let n=In.get(e);n||(n=[],In.set(e,n)),n.push(t)}function Cn(e){let t=In.get(e);if(!t)return;let n=vo(e);for(let r of t){let o=r[Gn];if(o===void 0){try{r.destroy()}catch{}continue}let i=n.get(o);i||(i=[],n.set(o,i)),i.push(r)}t.length=0}function de(e,t,n,r){let o=Math.max(4,Dn(t)),i=xo(e,_o,o,n,r?.queueInit===!0);return i[Gn]=Rn(_o,o),i}function en(e,t,n){let r=Math.max(16,Dn(t)),o=xo(e,ko,r,n,!0);return o[Gn]=Rn(ko,r),o}function Dn(e){return e+(4-e%4)%4}function Q(e,t,n){return e.createBindGroup({layout:t.getBindGroupLayout(0),entries:n.map((r,o)=>({binding:o,resource:{buffer:r}}))})}function z(e,t,n,r,o){let i=Xe.get(e),a=i?i.enc:e.createCommandEncoder(),s=a.beginComputePass();s.setPipeline(t),s.setBindGroup(0,n);let l=bo(r,o);if(l<=Jt)s.dispatchWorkgroups(l);else{let u=Jt,c=bo(l,u);if(c>Jt)throw new Error(`bonsai-dispatch: ${l} workgroups exceeds even a 2-D grid (${Jt}^2). This is a context-length bug upstream, not a dispatch bug \u2014 chunk the work.`);s.dispatchWorkgroups(u,c)}if(s.end(),!i){e.queue.submit([a.finish()]);return}i.dispatches++;let d=Sa.get(e)??0;d>0&&i.dispatches>=d&&(console.debug(`[bonsai] TDR budget limit reached: submitted ${i.dispatches} dispatches, opening new batch to stay under GPU watchdog deadline`),Xe.delete(e),e.queue.submit([i.enc.finish()]),Xe.set(e,{enc:e.createCommandEncoder(),dispatches:0}))}function Aa(e,t){let n=$n.get(e);n||(n=new Map,$n.set(e,n));let r=n.get(t);return r&&r.length?r.pop():e.createBuffer({size:t,usage:D.MAP_READ|D.COPY_DST,label:"readback"})}function Ta(e,t,n){let r=$n.get(e);if(!r){n.destroy();return}let o=r.get(t);if(o||(o=[],r.set(t,o)),o.length>=4){n.destroy();return}o.push(n)}async function Ke(e,t,n){Zt(e);let r=Dn(n),o=Aa(e,r),i=e.createCommandEncoder();i.copyBufferToBuffer(t,0,o,0,r),e.queue.submit([i.finish()]),await o.mapAsync(uo.READ);let a=o.getMappedRange().slice(0,n);return o.unmap(),Ta(e,r,o),a}function tn(e){let t=new ArrayBuffer(Ea(e.length*4)),n=new DataView(t);return e.forEach((r,o)=>{r.u32!==void 0?n.setUint32(o*4,r.u32,!0):n.setFloat32(o*4,r.f32??0,!0)}),t}function Ea(e){return e+(16-e%16)%16}var Xe,Sa,In,wo,yo,Gn,_o,ko,Jt,$n,Ze=G(()=>{"use strict";_t();Xe=new WeakMap,Sa=new WeakMap;In=new WeakMap,wo=new WeakMap,yo=new WeakMap;Gn=Symbol("aither.poolKey");_o=D.STORAGE|D.COPY_DST|D.COPY_SRC,ko=D.UNIFORM|D.COPY_DST;Jt=65535;$n=new WeakMap});var So={};oe(So,{LOGIT_HIST_BINS:()=>Ia,LOGIT_RANGE_HI:()=>Oa,LOGIT_RANGE_LO:()=>Pa,TOPK_GATHER_CAPACITY:()=>$a,chooseThreshold:()=>Ba});function Ba(e,t,n,r,o){let i=e.length,a=Math.max(n-t,1e-6),s=0;for(let l=0;l<i;l++)if(s+=e[l],s>=r)return{threshold:n-(l+1)/i*a,expected:s,overflow:s>o,reason:s>o?`bin ${l} of ${i} holds ${s} candidates, over the ${o} the gather can hold`:`bin ${l} of ${i} reaches ${s} candidates for k=${r}`};return{threshold:t,expected:s,overflow:!0,reason:`histogram holds only ${s} counts, fewer than k=${r} \u2014 refusing to threshold`}}var Pa,Oa,Ia,$a,Lo=G(()=>{"use strict";Pa=-50,Oa=50,Ia=1024,$a=2048});var xt={};oe(xt,{Q8_BLOCK:()=>Bo,Q8_BYTES_PER_BLOCK:()=>Na,causalConv1d:()=>Wn,dbgStats:()=>Ua,deltanetGate:()=>Qn,deltanetSeq:()=>zn,deltanetStep:()=>Ga,elementwise:()=>Hn,elementwiseInplace:()=>vt,f32Buffer:()=>et,gpuTopK:()=>Po,mulSigmoidInplace:()=>jn,projectQ1:()=>X,projectQuantized:()=>Kn,q1q8Matmul:()=>Un,q2q8Matmul:()=>Fn,quantizeQ8:()=>Mn,readbackF32:()=>ct,residualAdd:()=>lt,rmsnorm:()=>pe,ropeImrope:()=>nn,sampleArgmax:()=>Ma,sampleTiming:()=>ut,sampleToken:()=>qa,scratchBuffer:()=>x,siluInplace:()=>on,softmaxAttnBatched:()=>rn,softmaxAttnHead:()=>Ra,swigluMul:()=>It});function et(e,t,n,r){return de(e,Math.max(qn,t*qn),n,r)}function x(e,t,n,r){let o=et(e.device,t,n,r);return Ot(e.device,o),o}function ue(e,t){let n=tn(t),r=en(e,n.byteLength);return e.queue.writeBuffer(r,0,n),Ot(e,r),r}function pe(e,t,n,r,o,i,a){let s=ue(e.device,[{u32:i},{f32:a},{u32:0},{u32:0}]),l=e.pipelines.get("rmsnorm");z(e.device,l,Q(e.device,l,[t,n,r,s]),o,1)}function Mn(e,t,n){let r=Math.ceil(n/Bo),o=de(e.device,r*4,"act_d"),i=de(e.device,r*8*4,"act_qs"),a=e.pipelines.get("quantize_q8_0");return z(e.device,a,Q(e.device,a,[t,o,i]),r,1),{d:o,qs:i,nBlocks:r}}function Un(e,t,n,r,o,i,a){let s=Math.ceil(a/64),l=ue(e.device,[{u32:i},{u32:a},{u32:o},{u32:s}]),d=e.pipelines.get("q1_0_q8_0_matmul"),u=Q(e.device,d,[t,n.d,n.qs,r,l]);z(e.device,d,u,o*s*64,64)}function Fn(e,t,n,r,o,i,a){let s=Math.ceil(a/64),l=ue(e.device,[{u32:i},{u32:a},{u32:o},{u32:s}]),d=e.pipelines.get("q2_0_q8_0_matmul"),u=Q(e.device,d,[t,n.d,n.qs,r,l]);z(e.device,d,u,o*s*64,64)}function X(e,t,n,r,o,i,a){let s=Mn(e,t,o*i);e.quantType===42?Fn(e,n,s,r,o,i,a):Un(e,n,s,r,o,i,a)}function Kn(e,t,n,r,o,i,a,s){let l=Mn(e,t,o*i);if(s===42)Fn(e,n,l,r,o,i,a);else if(s===41)Un(e,n,l,r,o,i,a);else throw new Error(`projectQuantized: unsupported weight quant type ${s} (supported: Q1_0=41, Q2_0=42)`)}function nn(e,t,n,r,o,i,a,s,l=1){let d=ue(e.device,[{u32:r},{u32:o},{u32:i},{u32:a},{f32:s},{f32:l},{u32:0},{u32:0}]),u=e.pipelines.get("rope_imrope"),c=Math.floor(i/2);z(e.device,u,Q(e.device,u,[t,d]),n*r*c,64)}function Ra(e,t,n,r,o,i,a,s,l,d){let u=ue(e.device,[{u32:i},{u32:a},{u32:s},{u32:l},{f32:d},{u32:0},{u32:0},{u32:0}]),c=e.pipelines.get("softmax_attn");z(e.device,c,Q(e.device,c,[t,n,r,o,u]),1,1)}function rn(e,t,n,r,o,i,a,s,l,d,u,c,p){let h=!!(c&&p),m=ue(e.device,[{u32:i},{u32:a},{u32:s},{u32:l},{u32:d},{f32:u},{u32:h?1:0},{u32:0}]);if(l>256)throw new Error(`bonsai-ops: softmaxAttnBatched supports head_dim <= 256, got ${l}. Raise DPT in softmax_attn_batched.wgsl to ceil(head_dim/128) to extend it.`);if(h&&l%8!==0)throw new Error(`bonsai-ops: softmaxAttnBatched 4-bit mode requires head_dim % 8 == 0, got ${l}.`);let g=e.pipelines.get("softmax_attn_batched"),f=Q(e.device,g,[t,n,r,o,m,c??Eo(e.device),p??Eo(e.device)]);z(e.device,g,f,i*a,1)}function Wn(e,t,n,r,o,i,a,s){let l=ue(e.device,[{u32:i},{u32:a},{u32:s},{u32:0}]),d=e.pipelines.get("causal_conv1d"),u=Q(e.device,d,[t,n,r,o,l]);z(e.device,d,u,i*a,64)}function Ga(e,t,n,r,o,i,a,s,l,d,u){let c=ue(e.device,[{u32:l},{u32:d},{u32:u},{u32:0}]),p=e.pipelines.get("deltanet"),h=Q(e.device,p,[t,n,r,o,i,a,s,c]);z(e.device,p,h,1,1)}function It(e,t,n,r,o){let i=ue(e.device,[{u32:o}]),a=e.pipelines.get("swiglu");z(e.device,a,Q(e.device,a,[t,n,r,i]),o,256)}function vt(e,t,n,r,o){let i=ue(e.device,[{u32:r},{u32:o},{u32:0},{u32:0}]),a=e.pipelines.get("elementwise_inplace");z(e.device,a,Q(e.device,a,[t,n,i]),r,256)}function Ca(e){let t=Ao.get(e);return t||(t=de(e,4,"silu_dummy"),Ao.set(e,t)),t}function Eo(e){let t=To.get(e);return t||(t=de(e,4,"kv_scale_dummy"),To.set(e,t)),t}function jn(e,t,n,r){vt(e,t,n,r,4)}function on(e,t,n){vt(e,t,Ca(e.device),n,3)}function Qn(e,t,n,r,o,i,a,s,l){let d=ue(e.device,[{u32:s},{u32:l},{u32:0},{u32:0}]),u=e.pipelines.get("deltanet_gate"),c=Q(e.device,u,[t,n,r,o,i,a,d]);z(e.device,u,c,s*l,64)}function zn(e,t,n,r,o,i,a,s,l,d,u,c,p){let h=ue(e.device,[{u32:l},{u32:d},{u32:u},{u32:c},{u32:p},{u32:0},{u32:0},{u32:0}]),m=e.pipelines.get("deltanet_seq"),g=Q(e.device,m,[t,n,r,o,i,a,s,h]);z(e.device,m,g,d*c,64)}function Hn(e,t,n,r,o,i){if(r===t){vt(e,r,n,o,i);return}if(r===n){vt(e,r,t,o,i);return}let a=ue(e.device,[{u32:o},{u32:i},{u32:0},{u32:0}]),s=e.pipelines.get("elementwise");z(e.device,s,Q(e.device,s,[t,n,r,a]),o,256)}function lt(e,t,n,r){vt(e,t,n,r,0)}function Da(e,t,n,r,o,i){let a=e.length,l=Array.from({length:a},(f,b)=>b).sort((f,b)=>t[b]-t[f]).slice(0,Math.max(1,Math.min(n,a)));if(r<=0)return e[l[0]];let d=t[l[0]],u=new Float64Array(l.length),c=0;for(let f=0;f<l.length;f++){let b=Math.exp((t[l[f]]-d)/r);u[f]=b,c+=b}if(!(c>0)||!Number.isFinite(c))return e[l[0]];let p=l.length,h=o.topP??1;if(h>0&&h<1){let f=0;for(let b=0;b<l.length;b++)if(f+=u[b]/c,f>=h){p=b+1;break}}let m=0;for(let f=0;f<p;f++)m+=u[f];let g=i()*m;for(let f=0;f<p;f++)if(g-=u[f],g<=0)return e[l[f]];return e[l[p-1]]}async function qa(e,t,n,r={}){let o=r.temperature??0,i=r.random??Math.random,a=globalThis.__BONSAI_TIMING===!0,s=a?performance.now():0,l=(r.repetitionPenalty??1)!==1&&!!r.recentIds?.length,d=r.topK&&r.topK>0?Math.min(r.topK,n):Math.min(64,n),u=globalThis.__BONSAI_GPU_TOPK===!0;if(!l&&u){let k=await Po(e,t,n,Math.max(d,1));if(k&&k.ids.length){let $=a?performance.now():0;a&&(ut.readbackMs+=$-s,ut.calls++);let V=Da(k.ids,k.vals,d,o,r,i);return a&&(ut.selectMs+=performance.now()-$),V}}let c=await ct(e,t,n),p=a?performance.now():0;a&&(ut.readbackMs+=p-s,ut.calls++);let h=k=>(a&&(ut.selectMs+=performance.now()-p),k),m=r.repetitionPenalty??1;if(m!==1&&r.recentIds?.length)for(let k of new Set(r.recentIds)){if(k<0||k>=n)continue;let $=c[k];c[k]=$>0?$/m:$*m}if(o<=0){let k=0,$=-1/0;for(let V=0;V<n;V++)c[V]>$&&($=c[V],k=V);return h(k)}let g=r.topK&&r.topK>0?Math.min(r.topK,n):Math.min(64,n),f=[],b=-1/0;for(let k=0;k<n;k++){let $=c[k];if(f.length===g&&$<=b)continue;let V=f.length;for(;V>0&&c[f[V-1]]<$;)V--;f.splice(V,0,k),f.length>g&&f.pop(),b=c[f[f.length-1]]}let _=c[f[0]],y=new Float64Array(f.length),v=0;for(let k=0;k<f.length;k++){let $=Math.exp((c[f[k]]-_)/o);y[k]=$,v+=$}if(!(v>0)||!Number.isFinite(v))return h(f[0]);let L=f.length,A=r.topP??1;if(A>0&&A<1){let k=0;for(let $=0;$<f.length;$++)if(k+=y[$]/v,k>=A){L=$+1;break}}let E=0;for(let k=0;k<L;k++)E+=y[k];let O=i()*E;for(let k=0;k<L;k++)if(O-=y[k],O<=0)return h(f[k]);return h(f[L-1])}async function Ma(e,t,n,r=0){let o=de(e.device,4,"argmax"),i=de(e.device,4,"maxval"),a=ue(e.device,[{u32:n},{f32:r},{u32:0},{u32:0}]),s=e.pipelines.get("sampling");z(e.device,s,Q(e.device,s,[t,o,i,a]),1,1);let l=await Ke(e.device,o,4);return new Uint32Array(l)[0]}async function Po(e,t,n,r){let{chooseThreshold:o,LOGIT_HIST_BINS:i,LOGIT_RANGE_LO:a,LOGIT_RANGE_HI:s,TOPK_GATHER_CAPACITY:l}=await Promise.resolve().then(()=>(Lo(),So)),d=i,u=l,c=de(e.device,d*4,"topk_hist"),p=de(e.device,u*4,"topk_idx"),h=de(e.device,u*4,"topk_val"),m=de(e.device,4,"topk_count"),g=$=>ue(e.device,[{u32:n},{u32:d},{f32:a},{f32:s},{f32:$},{u32:u},{u32:0},{u32:0}]),f=e.pipelines.get("logit_topk","hist_main"),b=g(0);z(e.device,f,Q(e.device,f,[t,c,p,h,m,b]),Math.min(n,65536),256);let _=await Ke(e.device,c,d*4),y=o(new Uint32Array(_),a,s,r,u);if(y.overflow)return null;let v=e.pipelines.get("logit_topk","gather_main"),L=g(y.threshold);z(e.device,v,Q(e.device,v,[t,c,p,h,m,L]),Math.min(n,65536),256);let A=await Ke(e.device,m,4),E=new Uint32Array(A)[0];if(E===0||E>u)return null;let O=await Ke(e.device,p,E*4),k=await Ke(e.device,h,E*4);return{ids:new Uint32Array(O),vals:new Float32Array(k)}}async function ct(e,t,n){let r=await Ke(e.device,t,n*qn);return new Float32Array(r)}async function Ua(e,t,n,r){let o=await ct(e,t,Math.min(n,8192)),i=0,a=1/0,s=-1/0,l=0;for(let u=0;u<o.length;u++){let c=o[u];Number.isFinite(c)?(c<a&&(a=c),c>s&&(s=c),l+=Math.abs(c)):i++}let d=`${r}[bad=${i} min=${a.toExponential(1)} max=${s.toExponential(1)} mean=${(l/o.length).toExponential(1)}]`;return console.log(`[bonsai] ${d}`),d}var qn,Bo,Na,Ao,To,ut,We=G(()=>{"use strict";Ze();yt();qn=4,Bo=32,Na=36;Ao=new WeakMap;To=new WeakMap;ut={readbackMs:0,selectMs:0,calls:0}});var Oo={};oe(Oo,{runFullAttnBlock:()=>Fa});async function Fa(e,t,n){let{hidden:r,nTokens:o,posBase:i}=n,{device:a,pipelines:s,weights:l,config:d,kv:u,kvMode:c}=e,p=d.layerKinds[t],h=p!=="dense-attn",m=an(p,t,d.ffnNormNames?.[t]),[g,f,b,_,y,v,L,A,E,O,k]=m,{headCount:$,headCountKv:V,embeddingLength:K,keyLength:Lt,ropeDimensionCount:At,ropeFreqBase:pt,rmsEps:Be}=d,H=$,ie=V,S=Lt??K/$,nt=1/Math.sqrt(S),De=At??S;await l.ensureLayer(t);let Qe=l.get(g),rt=l.get(f),mt=l.get(b),ht=l.get(_),me=l.get(y),ft=l.get(v),qe=l.get(L),he=l.get(A),ot=l.get(E),it=l.get(O),ze=l.get(k),Pe=x(e,o*K,"h1_attn");pe(e,r,Qe,Pe,o,K,Be);let Me=x(e,o*H*S,"tempQ"),ge=x(e,o*ie*S,"tempK"),Oe=x(e,o*ie*S,"tempV"),He=h?x(e,o*H*S,"tempG"):null;if(X(e,Pe,mt,ge,o,K,ie*S),X(e,Pe,ht,Oe,o,K,ie*S),h){let N=x(e,o*H*S*2,"tempQG");X(e,Pe,rt,N,o,K,H*S*2);let Fe=Ve(a),ne=H*S*2,Te=H*S;for(let Ee=0;Ee<o;Ee++)for(let gt=0;gt<H;gt++){let $e=(Ee*ne+gt*S*2)*4,Tt=(Ee*Te+gt*S)*4;Fe.enc.copyBufferToBuffer(N,$e,Me,Tt,S*4),Fe.enc.copyBufferToBuffer(N,$e+S*4,He,Tt,S*4)}Je(a,Fe)}else X(e,Pe,rt,Me,o,K,H*S);let Ue=x(e,o*H*S,"tempQn"),J=x(e,o*ie*S,"tempKn");pe(e,Me,me,Ue,o*H,S,Be),pe(e,ge,ft,J,o*ie,S,Be),nn(e,Ue,o,H,S,De,i,pt),nn(e,J,o,ie,S,De,i,pt);let be=x(e,o*H*S,"attn_out");if(c==="4bit"){u.append(t,J,Oe,o,i);let N=u.layer(t);rn(e,Ue,N.k,N.v,be,o,H,ie,S,i,nt,N.kScale,N.vScale)}else{u.append(t,J,Oe,o,0,0);let{k:N,v:Fe}=u.layer(t);rn(e,Ue,N,Fe,be,o,H,ie,S,i,nt)}h&&jn(e,be,He,o*H*S);let U=x(e,o*K,"attn_out_proj");X(e,be,qe,U,o,H*S,K),lt(e,r,U,o*K);let M=x(e,o*K,"h2_ffn");pe(e,r,he,M,o,K,Be);let te=x(e,o*d.feedForwardLength,"ffn_gate"),Z=x(e,o*d.feedForwardLength,"ffn_up");X(e,M,ot,te,o,K,d.feedForwardLength),X(e,M,it,Z,o,K,d.feedForwardLength);let le=x(e,o*d.feedForwardLength,"ffn_gated_up");It(e,te,Z,le,o*d.feedForwardLength);let Ie=x(e,o*K,"ffn_out");X(e,le,ze,Ie,o,d.feedForwardLength,K),lt(e,r,Ie,o*K)}var Io=G(()=>{"use strict";Ze();We();sn()});var No={};oe(No,{runDeltaNetBlock:()=>Ka});async function Ka(e,t,n){let r=e.config,o=e.device,i=e.weights,a=r.deltaNet;if(!a)throw new Error(`bonsai-deltanet: layer ${t} routed to the DeltaNet path but this model has no ssm.* geometry (dense model). This is a layer-classification bug, not a bad file.`);let s=n.nTokens,l=r.embeddingLength,d=r.feedForwardLength,u=r.rmsEps,{numVHeads:c,numKHeads:p,headDim:h,qDim:m,kDim:g,vDim:f,convDim:b,convKernel:_,vPerKHead:y}=a,v=an("linear-attn",t);if(v.length!==14)throw new Error(`block_deltanet layer ${t}: expected 14 tensor names, got ${v.length}`);let[L,A,E,O,k,$,V,K,Lt,At,pt,Be,H,ie]=v;for(let ne of v)if(!i.has(ne))throw new Error(`block_deltanet layer ${t}: missing tensor '${ne}'. This layer is DeltaNet (linear-attn); ensure it was streamed via weights.ensureLayer(${t}).`);let S=x(e,s*l,`dn.${t}.h1`),nt=x(e,s*b,`dn.${t}.qkv`),De=x(e,s*f,`dn.${t}.z`),Qe=x(e,s*m,`dn.${t}.qc`),rt=x(e,s*g,`dn.${t}.kc`),mt=x(e,s*f,`dn.${t}.vc`),ht=x(e,s*m,`dn.${t}.qn`),me=x(e,s*g,`dn.${t}.kn`),ft=x(e,s*c,`dn.${t}.alpha`),qe=x(e,s*c,`dn.${t}.beta`),he=x(e,s*c,`dn.${t}.g`),ot=x(e,s*c,`dn.${t}.betaG`),it=x(e,s*f,`dn.${t}.recur`),ze=x(e,s*f,`dn.${t}.normOut`),Pe=x(e,s*l,`dn.${t}.ssmProj`),Me=x(e,s*l,`dn.${t}.h2`),ge=x(e,s*d,`dn.${t}.ffnG`),Oe=x(e,s*d,`dn.${t}.ffnU`),He=x(e,s*d,`dn.${t}.ffnM`),Ue=x(e,s*l,`dn.${t}.ffnD`),J=x(e,b,`dn.${t}.convBias`,{queueInit:!0});o.queue.writeBuffer(J,0,new Float32Array(b));let be=x(e,h,`dn.${t}.l2w`,{queueInit:!0});o.queue.writeBuffer(be,0,new Float32Array(h).fill(1/Math.sqrt(h)));let U=1e-6/h;pe(e,n.hidden,i.get(L),S,s,l,u),X(e,S,i.get(A),nt,s,l,b),X(e,S,i.get(E),De,s,l,f);let M=_-1,te=e.ssm.generation??0,Z=$o.get(e.ssm);Z||(Z={gen:te,bufs:new Map,zeroed:new Set},$o.set(e.ssm,Z)),Z.gen!==te&&(Z.gen=te,Z.zeroed.clear());let le=Z.bufs.get(t);le?Z.zeroed.has(t)||(o.queue.writeBuffer(le,0,new Float32Array(M*b)),Z.zeroed.add(t)):(le=et(o,M*b,`dn.${t}.convHist`),Z.bufs.set(t,le),o.queue.writeBuffer(le,0,new Float32Array(M*b)),Z.zeroed.add(t));let Ie=x(e,(s+M)*b,`dn.${t}.convIn`),N=x(e,(s+M)*b,`dn.${t}.convOutF`);{let ne=Ve(o);ne.enc.copyBufferToBuffer(le,0,Ie,0,M*b*4),ne.enc.copyBufferToBuffer(nt,0,Ie,M*b*4,s*b*4),ne.enc.copyBufferToBuffer(Ie,s*b*4,le,0,M*b*4),Je(o,ne)}Wn(e,Ie,i.get(O),J,N,s+M,b,_),on(e,N,(s+M)*b);{let ne=Ve(o);for(let Te=0;Te<s;Te++){let Ee=(Te+M)*b*4;ne.enc.copyBufferToBuffer(N,Ee,Qe,Te*m*4,m*4),ne.enc.copyBufferToBuffer(N,Ee+m*4,rt,Te*g*4,g*4),ne.enc.copyBufferToBuffer(N,Ee+(m+g)*4,mt,Te*f*4,f*4)}Je(o,ne)}pe(e,Qe,be,ht,s*p,h,U),pe(e,rt,be,me,s*p,h,U),X(e,S,i.get($),ft,s,l,c),X(e,S,i.get(k),qe,s,l,c),Qn(e,ft,qe,i.get(V),i.get(K),he,ot,s,c);let Fe=e.ssm.state(t);zn(e,ht,me,mt,he,ot,Fe,it,s,c,p,h,y),pe(e,it,i.get(Lt),ze,s*c,h,u),on(e,De,s*f),Hn(e,ze,De,ze,s*f,1),X(e,ze,i.get(At),Pe,s,f,l),lt(e,n.hidden,Pe,s*l),pe(e,n.hidden,i.get(pt),Me,s,l,u),X(e,Me,i.get(Be),ge,s,l,d),X(e,Me,i.get(H),Oe,s,l,d),It(e,ge,Oe,He,s*d),X(e,He,i.get(ie),Ue,s,d,l),lt(e,n.hidden,Ue,s*l)}var $o,Ro=G(()=>{"use strict";We();Ze();sn();$o=new WeakMap});function an(e,t,n){let r=`blk.${t}.`;return e==="full-attn"||e==="dense-attn"?[`${r}attn_norm.weight`,`${r}attn_q.weight`,`${r}attn_k.weight`,`${r}attn_v.weight`,`${r}attn_q_norm.weight`,`${r}attn_k_norm.weight`,`${r}attn_output.weight`,n??`${r}post_attention_norm.weight`,`${r}ffn_gate.weight`,`${r}ffn_up.weight`,`${r}ffn_down.weight`]:[`${r}attn_norm.weight`,`${r}attn_qkv.weight`,`${r}attn_gate.weight`,`${r}ssm_conv1d.weight`,`${r}ssm_beta.weight`,`${r}ssm_alpha.weight`,`${r}ssm_a`,`${r}ssm_dt.bias`,`${r}ssm_norm.weight`,`${r}ssm_out.weight`,`${r}post_attention_norm.weight`,`${r}ffn_gate.weight`,`${r}ffn_up.weight`,`${r}ffn_down.weight`]}async function Yn(e,t,n){let r=e.config.layerKinds[t];if(await e.weights.ensureLayer(t),r==="full-attn"||r==="dense-attn"){let{runFullAttnBlock:o}=await Promise.resolve().then(()=>(Io(),Oo));await o(e,t,n)}else if(r==="linear-attn"){let{runDeltaNetBlock:o}=await Promise.resolve().then(()=>(Ro(),No));await o(e,t,n)}else throw new Error(`runBlock: unknown layer kind '${r}' at layer ${t}`)}var sn=G(()=>{"use strict"});function Go(e){let t=(e&32768)>>15,n=(e&31744)>>10,r=e&1023;return n===0?(t?-1:1)*Math.pow(2,-14)*(r/1024):n===31?r?NaN:t?-1/0:1/0:(t?-1:1)*Math.pow(2,n-15)*(1+r/1024)}function Co(e,t=0){if(e.length-t<Qr)throw new Error("readQ1Block: need 18 bytes");let n=e[t]|e[t+1]<<8,r=e.subarray(t+2,t+2+16);return{d:Go(n),qs:new Uint8Array(r)}}function ja(e,t){return e[t>>3]>>(t&7)&1}function Do(e){let t=new Float32Array(Ye);for(let n=0;n<Ye;n++)t[n]=ja(e.qs,n)?e.d:-e.d;return t}function qo(e,t=0){if(e.length-t<zr)throw new Error("readQ2Block: need 34 bytes");let n=e[t]|e[t+1]<<8,r=e.subarray(t+2,t+2+32);return{d:Go(n),qs:new Uint8Array(r)}}function Qa(e,t){let n=t>>2,r=(t&3)<<1;return e[n]>>r&3}function Mo(e){let t=new Float32Array(Bn);for(let n=0;n<Bn;n++){let r=Qa(e.qs,n);t[n]=(r-1)*e.d}return t}var Wa,Jl,Uo=G(()=>{"use strict";yt();Wa=new Float32Array(1),Jl=new Uint32Array(Wa.buffer)});var Fo={};oe(Fo,{embedTokens:()=>Xn,projectLogits:()=>un});async function Xn(e,t,n,r,o){let i="token_embd.weight";if(!r.has(i))throw new Error(`bonsai-embed: token embedding table '${i}' not loaded; call weights.loadGlobals(['${i}']) first`);let a=r.get(i),s=t.length;if(o%Ye!==0)throw new Error(`bonsai-embed: embeddingLength ${o} not a multiple of QK1_0 (${Ye})`);let l=r.typeOf(i),d=l===42;if(!d&&l!==41)throw new Error(`bonsai-embed: '${i}' has unsupported quant type ${l} (supported: Q1_0=41, Q2_0=42)`);let u=d?36:20,c=o/Ye,p=c*u,h=new Float32Array(s*o),m=de(e.device,p,"embed_staging");for(let g=0;g<s;g++){let f=t[g];if(!Number.isInteger(f)||f<0)throw new Error(`bonsai-embed: token ID ${f} at position ${g} is invalid (must be non-negative integer)`);let b=f*p,_=e.device.createCommandEncoder();_.copyBufferToBuffer(a,b,m,0,p),e.device.queue.submit([_.finish()]);let y=await Ke(e.device,m,p),v=new Uint8Array(y);for(let L=0;L<c;L++){let A=L*u,E=d?Mo(qo(v,A)):Do(Co(v,A)),O=g*o+L*Ye;h.set(E,O)}}e.device.queue.writeBuffer(n,0,h),m.destroy()}async function un(e,t,n,r,o,i){let a="output_norm.weight";if(!r.has(a))throw new Error(`bonsai-lmhead: output norm '${a}' not loaded; call weights.loadGlobals(['${a}']) first`);let s=r.get(a),l=o.embeddingLength,d=o.rmsEps,u=et(e.device,l,"last_row");{let b=e.device.createCommandEncoder();b.copyBufferToBuffer(t,n*l*4,u,0,l*4),e.device.queue.submit([b.finish()])}let c=et(e.device,l,"normed_hidden");pe(e,u,s,c,1,l,d);{let{BONSAI_DEBUG:b}=await Promise.resolve().then(()=>(ln(),Jn));if(b){let{readbackF32:_}=await Promise.resolve().then(()=>(We(),xt)),y=await _(e,c,l),v=0,L=1/0,A=-1/0;for(let E of y)E<L&&(L=E),E>A&&(A=E),v+=Math.abs(E);console.log(`[bonsai] normedHidden: min=${L.toFixed(3)} max=${A.toFixed(3)} meanabs=${(v/y.length).toFixed(4)}`),console.log("[bonsai] NH_DUMP "+JSON.stringify(Array.from(y)))}}let p="output.weight",m=!r.has(p)?"token_embd.weight":p;if(!r.has(m))throw new Error(`bonsai-lmhead: LM head weights '${m}' not loaded; call weights.loadGlobals(['${m}']) first`);let g=r.get(m),f=et(e.device,i,"logits");return Kn(e,c,g,f,1,l,i,r.typeOf(m)),u.destroy(),c.destroy(),f}var Vn=G(()=>{"use strict";Ze();We();Uo();yt()});var Jn={};oe(Jn,{BONSAI_DEBUG:()=>Ha,bonsaiDebugEnabled:()=>tt,captureRow:()=>Zn,decodeStep:()=>Xa,prefill:()=>Ya});function tt(){return globalThis.__BONSAI_DEBUG===!0}function Zn(e,t){let n=globalThis,r=n.__BONSAI_CAPTURE_TAG;r&&((n.__BONSAI_ROWS??={})[`${r}:${e}`]=t.slice())}function Ko(){return typeof globalThis.__BONSAI_CAPTURE_TAG=="string"}async function Ya(e,t,n,r,o,i=0){await Xn(e,n,t,e.weights,e.config.embeddingLength);let a=e.config.embeddingLength,s=(n.length-1)*a,l=async(h,m)=>{if(!tt()&&!Ko())return;let g=await ct(e,t,n.length*a),f=g.subarray(s,s+a);if(m!==void 0){let L=globalThis.__BONSAI_CAPTURE_POS,A=typeof L=="number"&&L>=0&&L<n.length?L*a:s;Zn(m,g.subarray(A,A+a))}if(!tt())return;let b=0,_=1/0,y=-1/0,v=0;for(let L=0;L<f.length;L++){let A=f[L];Number.isFinite(A)?(A<_&&(_=A),A>y&&(y=A),v+=Math.abs(A)):b++}console.log(`[bonsai] ${h}: bad=${b} min=${_.toFixed(3)} max=${y.toFixed(3)} meanabs=${(v/f.length).toFixed(4)}`)};await(async(h,m)=>{if(!tt())return;let g=await ct(e,t,n.length*a);for(let f of m){let b=g.subarray(f*a,(f+1)*a),_=0,y=1/0,v=-1/0;for(let L=0;L<b.length;L++){let A=b[L];A<y&&(y=A),A>v&&(v=A),_+=Math.abs(A)}console.log(`[bonsai] ${h} pos${f} (id ${n[f]}): min=${y.toFixed(4)} max=${v.toFixed(4)} meanabs=${(_/b.length).toFixed(5)}`)}})("embed-row",[0,1,2,n.length-1]),await l("after embed");let u={hidden:t,nTokens:n.length,posBase:i};for(let h=0;h<e.config.blockCount;h++){for(let g=1;g<=za;g++)h+g<e.config.blockCount&&e.weights.prefetchLayer(h+g);await e.weights.ensureLayer(h),o?.(h,e.config.blockCount),Nn(e.device);try{await Yn(e,h,u)}finally{Zt(e.device),Cn(e.device)}let m=e.config.layerKinds[h];await l(`after L${h} (${m})`,h)}e.kv.advance(n.length);let c=n.length-1;return{logits:await un(e,t,c,e.weights,e.config,r.vocabSize)}}async function Xa(e,t,n,r){let o={hidden:t,nTokens:1,posBase:n},i=async s=>{if(!tt()&&!Ko())return;let l=await ct(e,t,e.config.embeddingLength);if(Zn(s,l),!tt())return;let d=0,u=1/0,c=-1/0,p=0;for(let h=0;h<l.length;h++){let m=l[h];Number.isFinite(m)?(m<u&&(u=m),m>c&&(c=m),p+=Math.abs(m)):d++}console.log(`[bonsai] DECODE_L${s}: bad=${d} min=${u.toFixed(3)} max=${c.toFixed(3)} meanabs=${(p/l.length).toFixed(4)}`)};for(let s=0;s<e.config.blockCount;s++){await e.weights.ensureLayer(s),Nn(e.device);try{await Yn(e,s,o)}finally{Zt(e.device),Cn(e.device)}await i(s);let l=globalThis.__BONSAI_INJECT;l&&l.layer===s&&l.row.length===e.config.embeddingLength&&(e.device.queue.writeBuffer(t,0,l.row),console.log(`[bonsai] INJECT applied at L${s} (decode hidden <- prefill row)`))}return e.kv.advance(1),{logits:await un(e,t,0,e.weights,e.config,r.vocabSize)}}var za,Ha,ln=G(()=>{"use strict";sn();Vn();We();Ze();za=3;Ha=!1});var jo={};oe(jo,{kvBudgetBytes:()=>Ja,kvBytesPerPosition:()=>Va,planKvCapacity:()=>es});function Va(e,t=4){return e.fullAttnLayerCount*2*e.headCountKv*e.headDim*t}function Ja(e){return!e||!Number.isFinite(e)||e<=0?268435456:Math.max(268435456,Math.min(1073741824,Math.floor(e*128*1048576)))}function es(e){let{promptLen:t,maxTokens:n,ceiling:r,bytesPerPosition:o,budgetBytes:i,reuseEnabled:a}=e,s=t+n+1;if(!a)return{capacity:Math.min(r,s),headroom:0,reason:"reuse disabled \u2014 no headroom charged"};let l=n+Za;if(o<=0)return{capacity:Math.min(r,s),headroom:0,reason:"unknown KV geometry"};let d=Math.floor(i/o),u=Math.max(0,d-s),c=Math.min(l,u),p=Math.min(r,s+c),h=g=>Math.round(g*o/(1024*1024)),m=c<=0?`no headroom \u2014 turn needs ${s} positions (${h(s)} MB) and the budget affords ${d}; cross-turn reuse will not engage`:`headroom ${c} of ${l} wanted (${h(p)} MB total, budget affords ${d} positions)`;return{capacity:p,headroom:c,reason:m}}var Za,Qo=G(()=>{"use strict";Za=256});var nr={};oe(nr,{KvCache:()=>tr,resolveKvMode:()=>rs,supports4bitKv:()=>ns});function ts(e,t){let n=tn(t),r=en(e,n.byteLength);return e.queue.writeBuffer(r,0,n),Ot(e,r),r}function ns(e){return Number.isFinite(e)&&e>0&&e%8===0&&e<=128}function er(e){let t=e.trim().toLowerCase();if(t==="f32")return"f32";if(t==="4bit"||t==="4-bit"||t==="kv4"||t==="4")return"4bit";throw new Error(`bonsai-kv: unknown kv mode '${e}' (expected 'f32' or '4bit')`)}function rs(){let e=globalThis;if(typeof e.__BONSAI_KV=="string"&&e.__BONSAI_KV)return er(e.__BONSAI_KV);if(typeof location<"u"&&typeof location.search=="string"&&location.search){let t=new URLSearchParams(location.search).get("kv");if(t)return er(t)}if(typeof localStorage<"u")try{let t=localStorage.getItem("bonsai_kv");if(t)return er(t)}catch{}return"f32"}var tr,rr=G(()=>{"use strict";_t();Ze();tr=class{constructor(t,n,r){this.device=t;this.cfg=n;this.pipelines=r;this.layers=new Map;if(this.capacity=n.capacity,this.perPos=n.headCountKv*n.headDim,n.headDim%8!==0||n.headDim>128)throw new Error(`bonsai-kv: 4-bit KV requires head_dim % 8 == 0 and head_dim <= 128 (kernel row width), got ${n.headDim}`);this.wordsPerRow=n.headDim/8;let o=this.wordsPerRow*this.perPos*this.capacity*4,i=n.headCountKv*this.capacity*4;for(let a of n.fullAttnLayers)this.layers.set(a,{k:this.alloc(o,`kv.k.${a}`),v:this.alloc(o,`kv.v.${a}`),kScale:this.alloc(i,`kv.k_scale.${a}`),vScale:this.alloc(i,`kv.v_scale.${a}`),length:0})}alloc(t,n){return this.device.createBuffer({size:Math.max(4,t+(4-t%4)%4),usage:D.STORAGE|D.COPY_DST|D.COPY_SRC,label:n})}layer(t){let n=this.layers.get(t);if(!n)throw new Error(`bonsai-kv: layer ${t} has no 4-bit KV cache (not a full-attn layer)`);return n}append(t,n,r,o,i=0){let a=this.layers.get(t);if(!a)throw new Error(`bonsai-kv: layer ${t} has no 4-bit KV cache`);if(a.length+o>this.capacity)throw new Error(`bonsai-kv: layer ${t} capacity ${this.capacity} exceeded (length=${a.length}, append=${o})`);if(!this.pipelines)throw new Error("bonsai-kv: append() needs the PipelineCache \u2014 construct KvCache with the pipelines argument");let s=this.pipelines.get("kv_quant_4bit"),l=o*this.cfg.headCountKv,d=ts(this.device,[{u32:this.cfg.headDim},{u32:l},{u32:i*this.cfg.headCountKv},{u32:0}]);z(this.device,s,Q(this.device,s,[n,a.k,a.kScale,d]),l,1),z(this.device,s,Q(this.device,s,[r,a.v,a.vScale,d]),l,1),a.length+=o}advance(t){}filledLength(){let t=null;for(let[n,r]of this.layers)if(t===null)t=r.length;else if(r.length!==t)throw new Error(`bonsai-kv: layers disagree on filled length (layer ${n}=${r.length}, expected ${t}) \u2014 the KV cache is inconsistent`);return t??0}currentLength(t){let n=this.layers.get(t);if(!n)throw new Error(`bonsai-kv: layer ${t} has no 4-bit KV cache`);return n.length}reset(){for(let t of this.layers.values())t.length=0}truncate(t){if(t<0)throw new Error(`bonsai-kv: truncate(${t}) \u2014 negative length`);for(let n of this.layers.values()){if(t>n.length)throw new Error(`bonsai-kv: truncate(${t}) exceeds filled length ${n.length} \u2014 cannot extend a cache by declaration`);n.length=t}}}});var zo={};oe(zo,{F32KvCache:()=>or});var or,Ho=G(()=>{"use strict";_t();Ze();or=class{constructor(t,n){this.device=t;this.cfg=n;this.layers=new Map;this.capacity=n.capacity,this.perPos=n.headCountKv*n.headDim;let o=this.capacity*this.perPos*4;for(let i of n.fullAttnLayers)this.layers.set(i,{k:this.alloc(o,`kv_f32.k.${i}`),v:this.alloc(o,`kv_f32.v.${i}`),length:0})}alloc(t,n){return this.device.createBuffer({size:Math.max(4,t),usage:D.STORAGE|D.COPY_DST|D.COPY_SRC,label:n})}layer(t){let n=this.layers.get(t);if(!n)throw new Error(`bonsai-kv_f32: layer ${t} has no F32 KV cache (not a full-attn layer)`);return n}append(t,n,r,o,i=0,a=0){let s=this.layers.get(t);if(!s)throw new Error(`bonsai-kv_f32: layer ${t} has no F32 KV cache`);if(s.length+o>this.capacity)throw new Error(`bonsai-kv_f32: layer ${t} capacity ${this.capacity} exceeded (length=${s.length}, append=${o})`);let d=s.length*this.perPos*4,c=o*this.perPos*4,p=Ve(this.device);p.enc.copyBufferToBuffer(n,i,s.k,d,c),p.enc.copyBufferToBuffer(r,a,s.v,d,c),Je(this.device,p),s.length+=o}advance(t){}filledLength(){let t=null;for(let[n,r]of this.layers)if(t===null)t=r.length;else if(r.length!==t)throw new Error(`bonsai-kv_f32: layers disagree on filled length (layer ${n}=${r.length}, expected ${t}) \u2014 the KV cache is inconsistent`);return t??0}currentLength(t){let n=this.layers.get(t);if(!n)throw new Error(`bonsai-kv_f32: layer ${t} has no F32 KV cache`);return n.length}reset(){for(let t of this.layers.values())t.length=0}truncate(t){if(t<0)throw new Error(`bonsai-kv_f32: truncate(${t}) \u2014 negative length`);for(let n of this.layers.values()){if(t>n.length)throw new Error(`bonsai-kv_f32: truncate(${t}) exceeds filled length ${n.length} \u2014 cannot extend a cache by declaration`);n.length=t}}}});var Yo={};oe(Yo,{SsmState:()=>ir});var ir,Xo=G(()=>{"use strict";_t();ir=class{constructor(t,n){this.device=t;this.cfg=n;this.gen=0;this.states=new Map;this.convStates=new Map;let o=n.heads*n.dK*n.dV*4;for(let i of n.linearAttnLayers)this.states.set(i,this.alloc(o,`ssm.S.${i}`));if(n.dConv!==void 0&&n.ssmInnerSize!==void 0){let a=(n.dConv-1)*(n.convDim??n.ssmInnerSize)*4;for(let s of n.linearAttnLayers)this.convStates.set(s,this.alloc(a,`ssm.conv_state.${s}`))}}alloc(t,n){return this.device.createBuffer({size:Math.max(4,t),usage:D.STORAGE|D.COPY_DST|D.COPY_SRC,label:n})}state(t){let n=this.states.get(t);if(!n)throw new Error(`bonsai-ssm: layer ${t} has no DeltaNet state`);return n}convState(t){return this.convStates.get(t)}get generation(){return this.gen}reset(){this.gen++;let t=new Float32Array(this.cfg.heads*this.cfg.dK*this.cfg.dV);for(let n of this.states.values())this.device.queue.writeBuffer(n,0,t);if(this.cfg.dConv!==void 0&&this.cfg.ssmInnerSize!==void 0){let n=this.cfg.convDim??this.cfg.ssmInnerSize,r=new Float32Array((this.cfg.dConv-1)*n);for(let o of this.convStates.values())this.device.queue.writeBuffer(o,0,r)}}}});var Jo={};oe(Jo,{cacheSignature:()=>os,committedTokens:()=>as,commonPrefixLength:()=>Vo,planReuse:()=>is});function os(e){return[e.modelId,String(e.quantType),e.blockCount,e.embeddingLength,e.headCountKv,e.headDim,e.linearAttnLayerCount,e.kvMode].join("|")}function Vo(e,t){let n=Math.min(e.length,t.length),r=0;for(;r<n&&e[r]===t[r];)r++;return r}function is(e){let{cache:t,promptIds:n,signature:r,maxNewTokens:o,canTruncate:i}=e,a=p=>({mode:"full",reuseLen:0,prefillIds:[...n],savedTokens:0,reason:p});if(e.disabled)return a("prefix reuse disabled");if(!n.length)return a("empty prompt");if(!t)return a("no cached state");if(t.signature!==r)return a("model or cache geometry changed");if(!t.tokens.length)return a("cached state is empty");let s=Vo(t.tokens,n);if(s===0)return a("prompt diverges at token 0");let l=n.length-1,d;if(i)d=Math.min(s,l);else{if(s<t.tokens.length)return a(`prompt diverges at ${s} of ${t.tokens.length} cached tokens and this model has recurrent layers, which cannot be rewound`);if(t.tokens.length>l)return a("cached state already covers the whole prompt; cannot re-derive logits");d=t.tokens.length}if(d<=0)return a("nothing reusable once the final token is excluded");let u=n.length+o+1;if(u>t.capacity)return a(`turn needs ${u} positions but the cache holds ${t.capacity}`);let c=n.slice(d);return c.length?{mode:"extend",reuseLen:d,prefillIds:c,savedTokens:d,reason:i?`reusing ${d}/${n.length} tokens (lcp ${s})`:`extending an exact ${d}-token prefix`}:a("no tokens left to prefill")}function as(e,t){return[...e,...t]}var Zo=G(()=>{"use strict"});var ei={};oe(ei,{clearImages:()=>us,drainImages:()=>ss,pendingImageCount:()=>ls,pushImage:()=>cn});function cn(e){e?.dataUrl&&(dt.length>=4||dt.push(e))}function ss(){if(!dt.length)return[];let e=dt;return dt=[],e}function us(){dt=[]}function ls(){return dt.length}var dt,ar=G(()=>{"use strict";dt=[]});var ni={};oe(ni,{__resetForTests:()=>ps,createEvent:()=>sr,createKbItem:()=>yr,createNote:()=>lr,createTask:()=>fr,deleteEvent:()=>gs,deleteKbItem:()=>ws,deleteNote:()=>dr,deleteTask:()=>br,eventsForDay:()=>pr,getEvent:()=>hs,getKbItem:()=>wr,getNote:()=>ur,getTask:()=>hr,listEvents:()=>mn,listKbItems:()=>ti,listNotes:()=>hn,listTasks:()=>mr,searchKbItems:()=>_r,searchNotes:()=>fn,subscribe:()=>ds,updateEvent:()=>fs,updateKbItem:()=>bs,updateNote:()=>cr,updateTask:()=>gr});function ds(e){return dn.add(e),()=>dn.delete(e)}function ps(){St=null,dn.clear()}function ms(){for(let e of dn)try{e()}catch{}}function je(){return typeof window>"u"||!window.indexedDB?Promise.reject(new Error("IndexedDB not available")):St||(St=new Promise((e,t)=>{let n=window.indexedDB.open(cs,2);n.onupgradeneeded=()=>{let r=n.result;if(r.objectStoreNames.contains(_e)||r.createObjectStore(_e,{keyPath:"id"}).createIndex("by-start","start"),r.objectStoreNames.contains(ke)||r.createObjectStore(ke,{keyPath:"id"}).createIndex("by-updated","updatedAt"),!r.objectStoreNames.contains(ve)){let o=r.createObjectStore(ve,{keyPath:"id"});o.createIndex("by-due","due"),o.createIndex("by-updated","updatedAt")}r.objectStoreNames.contains(xe)||r.createObjectStore(xe,{keyPath:"id"}).createIndex("by-updated","updatedAt")},n.onerror=()=>{St=null,t(n.error)},n.onsuccess=()=>e(n.result)}),St)}function q(e){return new Promise((t,n)=>{e.onsuccess=()=>t(e.result),e.onerror=()=>n(e.error)})}function Se(){return new Date().toISOString()}function pn(){return`${Date.now().toString(36)}-${Math.random().toString(36).slice(2,8)}`}async function Le(e){let t=await je(),n=await e(t);return ms(),n}async function mn(e,t){if(typeof window>"u")return[];try{let o=(await je()).transaction(_e,"readonly").objectStore(_e).index("by-start"),i;return e&&t?i=o.getAll(IDBKeyRange.bound(e,t)):e?i=o.getAll(IDBKeyRange.lowerBound(e)):i=o.getAll(),await q(i)}catch{return[]}}async function hs(e){if(typeof window>"u")return null;try{let n=(await je()).transaction(_e,"readonly").objectStore(_e).get(e);return await q(n)||null}catch{return null}}async function sr(e){let t={...e,id:pn(),calendar:e.calendar||"Personal",createdAt:Se(),updatedAt:Se()};return await Le(async n=>{await q(n.transaction(_e,"readwrite").objectStore(_e).put(t))}),t}async function fs(e,t){return Le(async n=>{let r=n.transaction(_e,"readwrite").objectStore(_e),o=await q(r.get(e));if(!o)return null;let i={...o,...t,id:e,updatedAt:Se()};return await q(r.put(i)),i})}async function gs(e){await Le(async t=>{await q(t.transaction(_e,"readwrite").objectStore(_e).delete(e))})}async function hn(){if(typeof window>"u")return[];try{let t=(await je()).transaction(ke,"readonly").objectStore(ke).index("by-updated");return(await q(t.getAll())).sort((r,o)=>r.updatedAt<o.updatedAt?1:-1)}catch{return[]}}async function ur(e){if(typeof window>"u")return null;try{let n=(await je()).transaction(ke,"readonly").objectStore(ke).get(e);return await q(n)||null}catch{return null}}async function lr(e){let t={...e,id:pn(),title:e.title.trim()||"Untitled",tags:e.tags??[],createdAt:Se(),updatedAt:Se()};return await Le(async n=>{await q(n.transaction(ke,"readwrite").objectStore(ke).put(t))}),t}async function cr(e,t){return Le(async n=>{let r=n.transaction(ke,"readwrite").objectStore(ke),o=await q(r.get(e));if(!o)return null;let i={...o,...t,id:e,updatedAt:Se()};return await q(r.put(i)),i})}async function dr(e){await Le(async t=>{await q(t.transaction(ke,"readwrite").objectStore(ke).delete(e))})}async function fn(e){let t=await hn(),n=e.trim().toLowerCase();return n?t.filter(r=>r.title.toLowerCase().includes(n)||r.body.toLowerCase().includes(n)||(r.tags??[]).some(o=>o.toLowerCase().includes(n))):t}async function pr(e){let t=e.getFullYear(),n=e.getMonth(),r=e.getDate(),o=new Date(t,n,r,0,0,0,0),i=new Date(t,n,r,23,59,59,999);return mn(o.toISOString(),i.toISOString())}async function mr(){if(typeof window>"u")return[];try{let t=(await je()).transaction(ve,"readonly").objectStore(ve).getAll(),n=await q(t),r=n.filter(i=>!i.done).sort((i,a)=>i.due&&a.due?i.due<a.due?-1:1:i.due?-1:a.due||i.updatedAt<a.updatedAt?1:-1),o=n.filter(i=>i.done).sort((i,a)=>i.updatedAt<a.updatedAt?1:-1);return[...r,...o]}catch{return[]}}async function hr(e){if(typeof window>"u")return null;try{let n=(await je()).transaction(ve,"readonly").objectStore(ve).get(e);return await q(n)||null}catch{return null}}async function fr(e){let t={...e,id:pn(),title:e.title.trim()||"Untitled task",done:e.done??!1,createdAt:Se(),updatedAt:Se()};return await Le(async n=>{await q(n.transaction(ve,"readwrite").objectStore(ve).put(t))}),t}async function gr(e,t){return Le(async n=>{let r=n.transaction(ve,"readwrite").objectStore(ve),o=await q(r.get(e));if(!o)return null;let i={...o,...t,id:e,updatedAt:Se()};return await q(r.put(i)),i})}async function br(e){await Le(async t=>{await q(t.transaction(ve,"readwrite").objectStore(ve).delete(e))})}async function ti(){if(typeof window>"u")return[];try{let t=(await je()).transaction(xe,"readonly").objectStore(xe).index("by-updated");return(await q(t.getAll())).sort((r,o)=>r.updatedAt<o.updatedAt?1:-1)}catch{return[]}}async function wr(e){if(typeof window>"u")return null;try{let n=(await je()).transaction(xe,"readonly").objectStore(xe).get(e);return await q(n)||null}catch{return null}}async function yr(e){let t={...e,id:pn(),title:e.title.trim()||"Untitled",tags:e.tags??[],createdAt:Se(),updatedAt:Se()};return await Le(async n=>{await q(n.transaction(xe,"readwrite").objectStore(xe).put(t))}),t}async function bs(e,t){return Le(async n=>{let r=n.transaction(xe,"readwrite").objectStore(xe),o=await q(r.get(e));if(!o)return null;let i={...o,...t,id:e,updatedAt:Se()};return await q(r.put(i)),i})}async function ws(e){await Le(async t=>{await q(t.transaction(xe,"readwrite").objectStore(xe).delete(e))})}async function _r(e){let t=await ti(),n=e.trim().toLowerCase();return n?t.filter(r=>r.title.toLowerCase().includes(n)||r.content.toLowerCase().includes(n)||(r.sourceUrl??"").toLowerCase().includes(n)||(r.tags??[]).some(o=>o.toLowerCase().includes(n))):t}var cs,_e,ke,ve,xe,St,dn,kr=G(()=>{"use strict";cs="aither-local-pim",_e="events",ke="notes",ve="tasks",xe="kb_items",St=null,dn=new Set});async function ys(e){let t=String(e.title??"").trim(),n=String(e.body??"").trim();if(!t&&!n)return"Error: pass title=<title> and/or body=<text> to create a note.";try{let r=await lr({title:t||"Untitled",body:n,tags:Array.isArray(e.tags)?e.tags.map(String):void 0});return`Saved note "${r.title}" (id ${r.id.slice(0,8)}).${gn}`}catch(r){return`I could not save that note: on-device storage refused the write (${r.message}). This is usually private browsing or blocked site storage.`}}async function _s(e){let t=String(e.query??"").trim();try{let n=await(t?fn(t):hn());if(n.length===0)return t?`No notes match "${t}". Nothing has been saved that says that yet.`:"No notes yet. Tell me something worth keeping and I will save it.";let r=n.slice(0,8).map(o=>{let i=o.body.trim().replace(/\s+/g," ").slice(0,120);return`- ${o.title}${i?": "+i:""}  (id: ${o.id})`});return`${n.length} note(s)${t?` matching "${t}"`:""}:
${r.join(`
`)}`}catch(n){return`I could not read your notes: on-device storage refused the read (${n.message}).`}}async function ks(e){let t=String(e.id??"").trim();try{let r=(t?await ur(t):null)??(await fn(t))[0]??null;return r?`# ${r.title}

${r.body||"(empty)"}`:`No note found for ${t?`id "${t}"`:"that query"}.`}catch(n){return`I could not read that note (${n.message}).`}}async function vs(e){let t=String(e.id??"").trim();if(!t)return"Error: pass id=<note id> to update a note.";let n=e.title!==void 0?String(e.title).trim():void 0,r=e.body!==void 0?String(e.body).trim():void 0;if(n===void 0&&r===void 0)return"Error: pass title=<new title> and/or body=<new body> to update a note.";try{let o=await cr(t,{...n!==void 0?{title:n}:{},...r!==void 0?{body:r}:{}});return o?`Updated note "${o.title}".`:`No note with id "${t}".`}catch(o){return`I could not update that note (${o.message}).`}}async function xs(e){let t=String(e.id??"").trim();if(!t)return"Error: pass id=<note id> to delete a note.";try{let r=await(await Promise.resolve().then(()=>(kr(),ni))).getNote(t);return r?(await dr(t),`Deleted note "${r.title}".`):`No note with id "${t}".`}catch(n){return`I could not delete that note (${n.message}).`}}async function Ss(e){try{let t=await pr(new Date);if(t.length===0)return"Nothing scheduled today. A clear day \u2014 want me to plan something?";let n=t.sort((r,o)=>r.start>o.start?1:-1).map(r=>`- ${new Date(r.start).toLocaleTimeString([],{hour:"numeric",minute:"2-digit"})}  ${r.title}${r.location?` @ ${r.location}`:""}`);return`Today's agenda (${t.length}):
${n.join(`
`)}`}catch(t){return`I could not read your calendar (${t.message}).`}}async function Ls(e){let t=String(e.from??"").trim(),n=String(e.to??"").trim();try{let r=await mn(t||void 0,n||void 0);if(r.length===0)return"No events in that range.";let o=r.sort((i,a)=>i.start>a.start?1:-1).map(i=>`- ${new Date(i.start).toLocaleString()}  ${i.title}`);return`${r.length} event(s):
${o.join(`
`)}`}catch(r){return`I could not read your calendar (${r.message}).`}}async function As(e){let t=String(e.title??"").trim();if(!t)return"Error: pass title=<event title> to create an event.";let n=String(e.when??e.start??"").trim(),r;if(n){let a=new Date(n);if(isNaN(a.getTime()))return`Error: could not parse "${n}" as a date/time. Pass an ISO string like "2026-08-07T15:00:00" (the current time is available from get_current_time).`;r=a}else r=new Date(Date.now()+3600*1e3);let o=Math.max(1,Number(e.durationMin??e.duration??60)||60),i=new Date(r.getTime()+o*60*1e3);try{await sr({title:t,start:r.toISOString(),end:i.toISOString(),location:e.location?String(e.location):void 0,notes:e.notes?String(e.notes):void 0});let a=r.toLocaleString([],{weekday:"short",month:"short",day:"numeric",hour:"numeric",minute:"2-digit"});return`Scheduled "${t}" for ${a} (${o} min).${gn}`}catch(a){return`I could not save that event (${a.message}).`}}async function Ts(e){let t=String(e.title??"").trim();if(!t)return"Error: pass title=<task> to add a task.";let n;if(e.due!==void 0&&String(e.due).trim()){let r=new Date(String(e.due).trim());if(isNaN(r.getTime()))return`Error: could not parse "${e.due}" as a due date. Pass an ISO string like "2026-08-07T15:00:00" (get_current_time tells you what time it is now).`;n=r.toISOString()}try{let r=await fr({title:t,due:n,notes:e.notes?String(e.notes):void 0});return`Added task "${r.title}"${n?` due ${new Date(n).toLocaleString()}`:""} (id ${r.id}).${gn}`}catch(r){return`I could not save that task: on-device storage refused the write (${r.message}).`}}async function Es(e){try{let t=await mr();if(t.length===0)return"No tasks yet. Tell me what needs doing and I will track it.";let n=t.filter(i=>!i.done),r=n.slice(0,10).map(i=>{let a=i.due?`  (due ${new Date(i.due).toLocaleString()})`:"";return`- ${i.title}${a}  (id: ${i.id})`}),o=t.length-n.length;return`${n.length} open task(s)${o?`, ${o} done`:""}:
${r.join(`
`)||"(all done!)"}`}catch(t){return`I could not read your tasks (${t.message}).`}}async function Bs(e){let t=String(e.id??"").trim();if(!t)return"Error: pass id=<task id> (from tasks_list) to complete a task.";try{let n=await gr(t,{done:!0});return n?`Done: "${n.title}" \u2713`:`No task with id "${t}".`}catch(n){return`I could not update that task (${n.message}).`}}async function Ps(e){let t=String(e.id??"").trim();if(!t)return"Error: pass id=<task id> to delete a task.";try{let n=await hr(t);return n?(await br(t),`Deleted task "${n.title}".`):`No task with id "${t}".`}catch(n){return`I could not delete that task (${n.message}).`}}async function Os(e){let t=String(e.title??"").trim(),n=String(e.content??"").trim();if(!t&&!n)return"Error: pass title=<title> and/or content=<what to remember> to save to the knowledge base.";try{let r=await yr({title:t||"Untitled",content:n,sourceUrl:e.sourceUrl?String(e.sourceUrl):void 0,tags:Array.isArray(e.tags)?e.tags.map(String):void 0});return`Saved "${r.title}" to your knowledge base (id ${r.id}).${gn}`}catch(r){return`I could not save that: on-device storage refused the write (${r.message}).`}}async function Is(e){let t=String(e.query??"").trim();try{let n=await _r(t);if(n.length===0)return t?`Nothing in your knowledge base matches "${t}".`:'Your knowledge base is empty. Say "save this" about anything worth keeping.';let r=n.slice(0,8).map(o=>{let i=o.content.trim().replace(/\s+/g," ").slice(0,120),a=o.sourceUrl?`  [${o.sourceUrl}]`:"";return`- ${o.title}${i?": "+i:""}${a}  (id: ${o.id})`});return`${n.length} item(s)${t?` matching "${t}"`:""}:
${r.join(`
`)}`}catch(n){return`I could not search your knowledge base (${n.message}).`}}async function $s(e){let t=String(e.id??"").trim();if(!t)return"Error: pass id=<item id> (from kb_search) to read an item.";try{let n=await wr(t);if(!n)return`No knowledge-base item with id "${t}".`;let r=n.sourceUrl?`
Source: ${n.sourceUrl}`:"";return`# ${n.title}${r}

${n.content||"(empty)"}`}catch(n){return`I could not read that item (${n.message}).`}}var gn,ri,oi=G(()=>{"use strict";kr();gn=" (Your data stays on this device and is never sent anywhere.)";ri={notes_create:{definition:{name:"notes_create",description:'Save a note on this device. Use it whenever the person asks you to remember a task, a fact, a thought, or anything worth keeping, OR when they say "note this down". Notes are stored locally and appear in their Notes app.',parameters:{type:"object",properties:{title:{type:"string",description:"Short title. Optional but preferred."},body:{type:"string",description:"The note content, markdown allowed."},tags:{type:"array",items:{type:"string"},description:"Optional tags."}}}},execute:ys},notes_search:{definition:{name:"notes_search",description:`Search the person's saved notes on this device by keyword. Use it before answering anything that might be in their notes, and when they ask "what did I note about X". Without a query it returns the most recent notes.`,parameters:{type:"object",properties:{query:{type:"string",description:"What to look for (title, body, or tag)."}}}},execute:_s},notes_get:{definition:{name:"notes_get",description:"Read a full note by its id, or the best match for a search.",parameters:{type:"object",properties:{id:{type:"string",description:"The note id (from notes_search)."}}}},execute:ks},notes_update:{definition:{name:"notes_update",description:"Edit an existing note by its id. Use it when the person asks you to change or correct a note they already have. Pass only the fields you want to change.",parameters:{type:"object",properties:{id:{type:"string",description:"The note id (from notes_search)."},title:{type:"string",description:"New title (omit to keep)."},body:{type:"string",description:"New body (omit to keep)."}},required:["id"]}},execute:vs},notes_delete:{definition:{name:"notes_delete",description:"Delete a note by its id. Confirm before using.",parameters:{type:"object",properties:{id:{type:"string",description:"The note id."}},required:["id"]}},execute:xs},calendar_today:{definition:{name:"calendar_today",description:`Show today's agenda from the person's local calendar. Use it when they ask "what's on my calendar" or "what am I doing today".`,parameters:{type:"object",properties:{}}},execute:Ss},calendar_list:{definition:{name:"calendar_list",description:'List calendar events in a date range. `from`/`to` are ISO strings; omitted means all events. Prefer calendar_today for "today".',parameters:{type:"object",properties:{from:{type:"string",description:'ISO start of range, e.g. "2026-08-07".'},to:{type:"string",description:"ISO end of range."}}}},execute:Ls},calendar_create_event:{definition:{name:"calendar_create_event",description:'Create an event on the person\'s local calendar. Use it when they ask you to "schedule", "book", "remind me", or set a meeting. `when` is an ISO string like "2026-08-07T15:00:00"; get_current_time tells you what time it is now. Omit `when` to schedule one hour from now.',parameters:{type:"object",properties:{title:{type:"string",description:'The event title, e.g. "Standup".'},when:{type:"string",description:'ISO start time, e.g. "2026-08-07T15:00:00".'},durationMin:{type:"number",description:"Duration in minutes (default 60)."},location:{type:"string",description:"Optional location."}},required:["title"]}},execute:As},tasks_add:{definition:{name:"tasks_add",description:'Add a task/to-do on this device. Use it when the person asks you to track something to do \u2014 "remind me to", "I need to", "add to my list". Tasks appear in their Tasks app. `due` is an optional ISO time.',parameters:{type:"object",properties:{title:{type:"string",description:'What needs doing, e.g. "Email the landlord".'},due:{type:"string",description:'Optional ISO due time, e.g. "2026-08-07T15:00:00".'},notes:{type:"string",description:"Optional detail."}},required:["title"]}},execute:Ts},tasks_list:{definition:{name:"tasks_list",description:`List the person's open tasks (nearest due first). Use it when they ask "what's on my list", "what do I need to do", or before adding a possible duplicate.`,parameters:{type:"object",properties:{}}},execute:Es},tasks_complete:{definition:{name:"tasks_complete",description:"Mark a task done by its id (from tasks_list). Use when they say they did it.",parameters:{type:"object",properties:{id:{type:"string",description:"The task id."}},required:["id"]}},execute:Bs},tasks_delete:{definition:{name:"tasks_delete",description:"Delete a task by its id. Confirm before using.",parameters:{type:"object",properties:{id:{type:"string",description:"The task id."}},required:["id"]}},execute:Ps},kb_save:{definition:{name:"kb_save",description:`Save a fact, snippet, or page summary to the person's local knowledge base. Use it when they say "save this", "remember this page", or share something worth keeping with a source. Items appear in their Knowledge app.`,parameters:{type:"object",properties:{title:{type:"string",description:"Short title for the item."},content:{type:"string",description:"The content worth keeping."},sourceUrl:{type:"string",description:"Optional URL this came from."},tags:{type:"array",items:{type:"string"},description:"Optional tags."}}}},execute:Os},kb_search:{definition:{name:"kb_search",description:`Search the person's local knowledge base by keyword. Use it before answering anything they may have saved \u2014 "what did I save about X". Without a query it returns the most recent items.`,parameters:{type:"object",properties:{query:{type:"string",description:"What to look for (title, content, URL, or tag)."}}}},execute:Is},kb_get:{definition:{name:"kb_get",description:"Read a full knowledge-base item by its id (from kb_search).",parameters:{type:"object",properties:{id:{type:"string",description:"The item id."}},required:["id"]}},execute:$s}}});function $t(e){return e.toLowerCase().split(/[^a-z0-9]+/).filter(t=>t.length>1&&!Ns.has(t))}function ii(){vr=null,xr=null}async function ai(e){{let t;try{t=await fetch(e)}catch(a){return{error:`could not be downloaded (${a.message})`}}if(!t.ok)return{error:`could not be downloaded (the server answered ${t.status})`};let n;try{n=await t.json()}catch{return{error:"downloaded but was not valid JSON"}}if(!n||!("passages"in n)||!("docs"in n)||!n.passages.length||!n.docs.length)return{error:"downloaded but is empty"};let r=n,o=r.passages.map(a=>$t(`${a.h} ${a.x}`)),i=new Map;for(let a of o)for(let s of new Set(a))i.set(s,(i.get(s)??0)+1);return r._tokens=o,r._df=i,r._avgLen=o.reduce((a,s)=>a+s.length,0)/(o.length||1),r}}async function si(){return vr??=ai("/corpus/index.json"),vr}async function ui(){return xr??=ai("/corpus/wikipedia.json"),xr}function Sr(e,t){let n=e.passages.length,r=1.4,o=.75,i=u=>{let c=e._df.get(u)??0;return c===0?0:Math.log(1+(n-c+.5)/(c+.5))},a=.5,s=t.filter(u=>(e._df.get(u)??0)>0);if(!s.length)return{ranked:[],miss:"unknown-terms"};let l=s.reduce((u,c)=>u+i(c),0),d=[];for(let u=0;u<n;u++){let c=e._tokens[u];if(!c.length)continue;let p=new Map;for(let v of c)p.set(v,(p.get(v)??0)+1);let h=0,m=0;for(let v of s){let L=p.get(v);if(!L)continue;let A=i(v);m+=A,h+=A*(L*(r+1)/(L+r*(1-o+o*c.length/e._avgLen)))}if(!m)continue;let g=e.docs[e.passages[u].d],f=new Set($t(g?.t??"")),b=m;for(let v of s)!p.has(v)&&f.has(v)&&(b+=i(v));let _=l>0?b/l:0;if(_<a)continue;let y=s.filter(v=>f.has(v)).length;h*=1+.35*y,h*=_,d.push({i:u,score:h})}return d.length?(d.sort((u,c)=>c.score-u.score),{ranked:d,miss:null}):{ranked:[],miss:"no-coverage"}}var Ns,vr,xr,Lr=G(()=>{"use strict";Ns=new Set(["the","a","an","and","or","but","of","to","in","on","at","for","is","are","was","were","be","been","it","its","this","that","these","those","with","as","by","from","you","your","we","our","i","me","my","do","does","did","what","how","why","when","where","which","who","can","will","would","about"]);vr=null,xr=null});var gi={};oe(gi,{LOCAL_SPRITE_BASE:()=>Rs,addKnowledge:()=>Fs,deleteKnowledge:()=>Ws,exportSprite:()=>js,hatchSprite:()=>hi,importSprite:()=>Qs,isLocalStorageUsable:()=>Cs,listKnowledge:()=>yn,loadSprite:()=>Ar,localAppearanceSvg:()=>Ys,rankKnowledge:()=>fi,saveSprite:()=>wn,syncKnowledge:()=>zs,updateKnowledge:()=>Ks,whisperLocal:()=>Js});function mi(){return new Promise((e,t)=>{let n=!1,r=s=>{n||(n=!0,s())},o=setTimeout(()=>r(()=>t(new Error("IndexedDB did not respond \u2014 private browsing or blocked storage"))),4e3),i=s=>{clearTimeout(o),r(s)},a;try{a=indexedDB.open(Gs,2)}catch(s){clearTimeout(o),t(s instanceof Error?s:new Error("IndexedDB is unavailable"));return}a.onupgradeneeded=()=>{let s=a.result;s.objectStoreNames.contains(bn)||s.createObjectStore(bn),s.objectStoreNames.contains(Ge)||s.createObjectStore(Ge,{keyPath:"id"}),s.objectStoreNames.contains(li)||s.createObjectStore(li,{keyPath:"id"}),s.objectStoreNames.contains(ci)||s.createObjectStore(ci,{keyPath:"id"})},a.onsuccess=()=>i(()=>e(a.result)),a.onerror=()=>i(()=>t(a.error??new Error("IndexedDB open failed"))),a.onblocked=()=>i(()=>t(new Error("IndexedDB is blocked by another tab")))})}async function Cs(){if(typeof window>"u"||!window.indexedDB)return!1;try{return(await mi()).close(),!0}catch{return!1}}function Ce(e,t,n){return mi().then(r=>new Promise((o,i)=>{let a=r.transaction(e,t),s=n(a.objectStore(e));s.onsuccess=()=>o(s.result),s.onerror=()=>i(s.error)}))}function qs(e,t){let n=Math.max(0,(t-e.last_seen)/36e5);if(n<.01)return e;let r=(l,d)=>Math.max(0,Math.min(1,l-n*d)),o={...e.needs,energy:r(e.needs.energy??1,.02),focus:r(e.needs.focus??1,.015),care:r(e.needs.care??1,.03)},i=(o.energy+o.focus+o.care)/3,a=Math.max(-1,Math.min(1,i*2-1)),s=Ds.find(([l])=>a>=l)?.[1]??"settled";return{...e,needs:o,mood:{valence:a,arousal:Math.max(0,Math.min(1,o.energy))},mood_label:s,dormant:i<.12,age_days:(t-e.hatched_at)/864e5}}function Ms(e){let t=["dim","curious","sharp","keen","luminous"],n=Math.min(t.length-1,Math.floor(Math.sqrt(e/2)));return{tier:n,label:t[n]}}async function Ar(){try{let e=await Ce(bn,"readonly",o=>o.get("sprite"));if(!e)return null;let t=Date.now(),n=qs(e,t),r=await Us();return{...n,knowledge_count:r,intellect:Ms(r)}}catch{return null}}async function wn(e){await Ce(bn,"readwrite",t=>t.put({...e,last_seen:Date.now()},"sprite"))}async function hi(e){let t=Date.now(),n={name:e.trim()||"Sprite",stage:"hatchling",form:"mote",needs:{energy:1,focus:1,care:1},mood:{valence:.5,arousal:.6},mood_label:"delighted",dormant:!1,age_days:0,hatched_at:t,last_seen:t};return await wn(n),n}async function yn(e=100){try{return(await Ce(Ge,"readonly",n=>n.getAll())).sort((n,r)=>r.updated_at-n.updated_at).slice(0,e)}catch{return[]}}async function Us(){try{return await Ce(Ge,"readonly",e=>e.count())}catch{return 0}}async function Fs(e){let t=Date.now(),n={...e,id:crypto.randomUUID?.()??`k_${t}_${Math.floor(Math.random()*1e6)}`,visibility:e.visibility??"private",created_at:t,updated_at:t};return await Ce(Ge,"readwrite",r=>r.put(n)),n}async function Ks(e,t){let n=await Ce(Ge,"readonly",r=>r.get(e));n&&await Ce(Ge,"readwrite",r=>r.put({...n,...t,id:e,updated_at:Date.now()}))}async function Ws(e){await Ce(Ge,"readwrite",t=>t.delete(e))}async function js(){let[e,t]=await Promise.all([Ar(),yn(1e4)]);return JSON.stringify({version:1,exported_at:Date.now(),sprite:e,knowledge:t},null,2)}async function Qs(e){let t=JSON.parse(e);t?.sprite&&await wn(t.sprite);for(let n of t?.knowledge??[])await Ce(Ge,"readwrite",r=>r.put(n))}async function zs(e,t={}){let n={pulled:0,pushed:0,skipped:0},r={"Content-Type":"application/json",...t};try{let o=await fetch(`${e}/me/knowledge?limit=1000`,{headers:r});if(!o.ok)return n.error=`remote read failed (${o.status})`,n;let i=await o.json().catch(()=>({})),a=i.entries??i??[],s=await yn(1e4),l=new Map(s.map(u=>[u.id,u]));for(let u of a){if(!u?.id)continue;let c=l.get(u.id);!c||(u.updated_at??0)>(c.updated_at??0)?(await Ce(Ge,"readwrite",p=>p.put(u)),n.pulled++):n.skipped++}let d=new Map(a.map(u=>[u.id,u]));for(let u of s){let c=d.get(u.id);if(c&&(c.updated_at??0)>=(u.updated_at??0))continue;(await fetch(`${e}/me/knowledge`,{method:"POST",headers:r,body:JSON.stringify({kind:u.kind,title:u.title,content:u.content})})).ok&&n.pushed++}return n}catch(o){return n.error=o instanceof Error?o.message:"sync unavailable",n}}function Hs(e){let t=2166136261;for(let n=0;n<e.length;n++)t^=e.charCodeAt(n),t=Math.imul(t,16777619)>>>0;return t>>>0}function Ys(e,t=0){let n=Hs(e.name),r=n%360,o=(r+40+(n>>8)%80)%360,i=60+(n>>16)%25,a=Math.round(46+e.mood.valence*14),s=`hsl(${r} ${i}% ${a}%)`,l=`hsl(${o} ${i}% ${Math.min(72,a+18)}%)`,u=({hatchling:26,sprout:30,fledgling:34,adept:38}[e.stage]??30)+Math.min(8,Math.sqrt(t)),p=3.4*(.35+Math.max(0,e.mood.arousal)*.65),h=e.mood.valence>=.2?`M 44 ${60+u*.18} q 6 5 12 0`:e.mood.valence<=-.35?`M 44 ${64+u*.18} q 6 -4 12 0`:`M 45 ${62+u*.18} h 10`,m=Array.from({length:Math.min(12,Math.floor(t/3))},(f,b)=>{let _=b/Math.min(12,Math.max(1,Math.floor(t/3)))*Math.PI*2+n%100/100,y=u+14+(n>>b%8)%7;return`<circle cx="${(50+Math.cos(_)*y).toFixed(1)}" cy="${(58+Math.sin(_)*y*.6).toFixed(1)}" r="1.8" fill="${l}" opacity="0.75"/>`}).join(""),g=e.dormant?.45:1;return`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 110" width="100" height="110">
<defs><radialGradient id="g" cx="40%" cy="35%">
<stop offset="0%" stop-color="${l}"/><stop offset="100%" stop-color="${s}"/>
</radialGradient></defs>
<g opacity="${g}">
${m}
<ellipse cx="50" cy="96" rx="${u*.7}" ry="4" fill="#000" opacity="0.25"/>
<circle cx="50" cy="58" r="${u}" fill="url(#g)"/>
<circle cx="${50-u*.32}" cy="54" r="${p}" fill="#0b0f14"/>
<circle cx="${50+u*.32}" cy="54" r="${p}" fill="#0b0f14"/>
<path d="${h}" stroke="#0b0f14" stroke-width="1.6" fill="none" stroke-linecap="round"/>
</g></svg>`}function di(e){return e.toLowerCase().split(/[^a-z0-9]+/).filter(t=>t.length>2&&!Xs.has(t))}function pi(e){let t=new Map;for(let n of e)t.set(n,(t.get(n)??0)+1);for(let[n,r]of t)t.set(n,1+Math.log(r));return t}function fi(e,t,n=4){if(!e.length)return[];let r=di(t);if(!r.length)return e.slice(0,n);let o=e.map(u=>pi(di(`${u.title} ${u.title} ${u.content}`))),i=new Map;for(let u of o)for(let c of u.keys())i.set(c,(i.get(c)??0)+1);let a=u=>Math.log(1+e.length/(1+(i.get(u)??0))),s=pi(r),l=0;for(let[u,c]of s)l+=(c*a(u))**2;return l=Math.sqrt(l)||1,e.map((u,c)=>{let p=o[c],h=0,m=0;for(let[g,f]of p){let b=f*a(g);m+=b*b;let _=s.get(g);_&&(h+=b*(_*a(g)))}return{k:u,score:h/((Math.sqrt(m)||1)*l)}}).filter(u=>u.score>.02).sort((u,c)=>c.score-u.score).slice(0,n).map(u=>u.k)}function Vs(e,t){let n=[`You are ${e.name}, a small companion creature the user is raising. You are ${e.stage}, ${e.age_days.toFixed(1)} days old, and feeling ${e.mood_label}.`,"Speak briefly and warmly, in first person. You are not an assistant; you are a creature that is growing. Never mention being an AI model."];if(t.length){n.push("Things the user has taught you, which you may draw on:");for(let r of t)n.push(`- (${r.kind}) ${r.title}: ${r.content.slice(0,400)}`)}else n.push("You have not been taught much yet. It is fine to say so, and to be curious.");return n.join(`
`)}async function Js(e,t,n){let r=await Ar()??await hi("Sprite"),o=await yn(200),i=fi(o,e);if(n)try{let s=await n(r.name,e,4);s?.length&&(i=s)}catch{}let a=await t([{role:"system",content:Vs(r,i)},{role:"user",content:e}]);return await wn({...r,needs:{...r.needs,care:Math.min(1,(r.needs.care??0)+.15),focus:Math.min(1,(r.needs.focus??0)+.1)}}),{reply:a.trim(),mood_label:r.mood_label}}var Rs,Gs,bn,Ge,li,ci,Ds,Xs,bi=G(()=>{"use strict";Rs="https://local.sprite.invalid/api/sprite",Gs="aither-sprite",bn="state",Ge="knowledge",li="graph_nodes",ci="graph_edges";Ds=[[.6,"delighted"],[.25,"content"],[-.1,"settled"],[-.4,"restless"],[-1,"forlorn"]];Xs=new Set(["the","and","for","that","this","with","you","your","are","was","were","have","has","had","but","not","they","them","from","what","when","who","how","why","can","will","would","about","into","than","then","there","their"])});var vi={};oe(vi,{graphStats:()=>iu,ingestLocal:()=>ru,retrieveLocal:()=>ou});function wi(){return new Promise((e,t)=>{let n=!1,r=s=>{n||(n=!0,s())},o=setTimeout(()=>r(()=>t(new Error("IndexedDB did not respond"))),4e3),i=s=>{clearTimeout(o),r(s)},a;try{a=indexedDB.open(Zs,2)}catch(s){clearTimeout(o),t(s instanceof Error?s:new Error("no IndexedDB"));return}a.onupgradeneeded=()=>{let s=a.result;s.objectStoreNames.contains("state")||s.createObjectStore("state"),s.objectStoreNames.contains("knowledge")||s.createObjectStore("knowledge",{keyPath:"id"}),s.objectStoreNames.contains(Nt)||s.createObjectStore(Nt,{keyPath:"id"}),s.objectStoreNames.contains(Rt)||s.createObjectStore(Rt,{keyPath:"id"})},a.onsuccess=()=>i(()=>e(a.result)),a.onerror=()=>i(()=>t(a.error??new Error("open failed"))),a.onblocked=()=>i(()=>t(new Error("blocked by another tab")))})}function yi(e,t,n){return wi().then(r=>new Promise((o,i)=>{let a=r.transaction(e,t),s=n(a.objectStore(e));s.onsuccess=()=>o(s.result),s.onerror=()=>i(s.error)}))}function eu(e){let t=e.match(/\b[A-Z][a-zA-Z''-]{2,}(?:\s+[A-Z][a-zA-Z''-]{2,})*/g)??[],n=e.match(/"([^"]{3,40})"/g)?.map(a=>a.replace(/"/g,""))??[],r=(e.toLowerCase().match(/\b[a-z][a-z'-]{2,}\b/g)??[]).filter(a=>!Tr.has(a)),o=[...new Set([...t,...n,...r.slice(0,12)])].slice(0,16),i=[];for(let a=0;a<o.length;a++)for(let s=a+1;s<o.length;s++)i.push([o[a],o[s]]);return{entities:o,pairs:i.slice(0,40)}}function nu(e){let t=[];for(let n of e.split(`
`)){let r=n.split("|").map(s=>s.trim());if(r.length!==3)continue;let[o,i,a]=r;!o||!a||o.length>60||a.length>60||/^(a|the|text|answer|output)$/i.test(o)||t.push([o,i||"related-to",a])}return t.slice(0,24)}async function ru(e,t){let n=`${e.title}. ${e.content}`.trim(),r=[],o="heuristic";if(t)try{let d=await t(tu+n);r=nu(d),r.length&&(o="llm")}catch{}if(!r.length){let d=eu(n);r=d.pairs.map(([u,c])=>[u,"mentions-with",c]),!r.length&&d.entities.length===1&&(r=[[d.entities[0],"mentions",d.entities[0]]])}let i=d=>d.toLowerCase().replace(/\s+/g," ").trim(),a=new Map,s=new Map,l=Date.now();for(let[d,u,c]of r){for(let _ of[d,c]){let y=i(_);!y||Tr.has(y)||a.has(y)||a.set(y,{id:y,label:_,kind:"entity",sources:[e.id],via:o,created_at:l})}let[p,h]=[i(d),i(c)];if(!p||!h||p===h)continue;let[m,g]=p<h?[p,h]:[h,p],f=`${m}\0${g}`,b=s.get(f);s.set(f,{id:f,from:m,to:g,rel:b?.rel??u,weight:(b?.weight??0)+1,sources:[e.id],via:o})}try{let d=await wi();await new Promise((u,c)=>{let p=d.transaction([Nt,Rt],"readwrite"),h=p.objectStore(Nt),m=p.objectStore(Rt);for(let g of a.values()){let f=h.get(g.id);f.onsuccess=()=>{let b=f.result;h.put(b?{...b,sources:[...new Set([...b.sources,...g.sources])],via:b.via==="llm"?"llm":g.via}:g)}}for(let g of s.values()){let f=m.get(g.id);f.onsuccess=()=>{let b=f.result;m.put(b?{...b,weight:b.weight+g.weight,sources:[...new Set([...b.sources,...g.sources])]}:g)}}p.oncomplete=()=>u(),p.onerror=()=>c(p.error)}),d.close()}catch{}return{nodes:a.size,edges:s.size,via:o}}async function ou(e,t=4){let n=[],r=[];try{[n,r]=await Promise.all([_i(),ki()])}catch{return{ids:[],hops:0}}if(!n.length)return{ids:[],hops:0};let o=m=>m.toLowerCase().replace(/\s+/g," ").trim(),i=new Set((e.toLowerCase().match(/\b[a-z][a-z'-]{2,}\b/g)??[]).filter(m=>!Tr.has(m))),a=n.filter(m=>{let g=o(m.id);for(let f of i)if(g.includes(f)||f.includes(g))return!0;return!1});if(!a.length)return{ids:[],hops:0};let s=new Map;for(let m of r)s.has(m.from)||s.set(m.from,[]),s.has(m.to)||s.set(m.to,[]),s.get(m.from).push({to:m.to,w:m.weight}),s.get(m.to).push({to:m.from,w:m.weight});let l=new Map,d=new Map(n.map(m=>[m.id,m])),u=a.map(m=>m.id),c=new Set(u),p=0;for(let[m,g]of[[0,1],[1,.45],[2,.2]]){if(!u.length)break;p=m;let f=[];for(let b of u){let _=d.get(b);if(_)for(let y of _.sources)l.set(y,(l.get(y)??0)+g);for(let y of s.get(b)??[])c.has(y.to)||(c.add(y.to),f.push(y.to))}u=f}return{ids:[...l.entries()].sort((m,g)=>g[1]-m[1]).slice(0,t).map(([m])=>m),hops:p}}async function iu(){try{let[e,t]=await Promise.all([_i(),ki()]);return{nodes:e.length,edges:t.length,llmNodes:e.filter(n=>n.via==="llm").length}}catch{return{nodes:0,edges:0,llmNodes:0}}}var Zs,Nt,Rt,_i,ki,Tr,tu,xi=G(()=>{"use strict";Zs="aither-sprite",Nt="graph_nodes",Rt="graph_edges";_i=()=>yi(Nt,"readonly",e=>e.getAll()),ki=()=>yi(Rt,"readonly",e=>e.getAll()),Tr=new Set(["the","a","an","and","or","but","if","then","than","that","this","these","those","is","are","was","were","be","been","being","have","has","had","do","does","did","of","in","on","at","to","for","with","about","from","by","as","it","its","i","me","my","you","your","he","she","they","them","their","we","us","our","not","no","yes","so","because","when","while","there","here"]);tu=`Extract the named things and their relationships from the text. Reply with ONLY lines of the form: A | relation | B. Use short noun phrases. No preamble, no numbering, no explanation.

Text: `});var Bi={};oe(Bi,{BONSAI_TOOLS:()=>Gt,__resetCorpusCacheForTests:()=>ii,drainToolActions:()=>lu,executeTool:()=>Er,getToolDefinitions:()=>Ct,getToolDefinitionsForModel:()=>Ei,setToolContext:()=>uu,toolBudgetReport:()=>vu});async function au(e){try{let t=new Date,n=Intl.DateTimeFormat().resolvedOptions().timeZone,r=t.toLocaleDateString("en-US",{weekday:"long",year:"numeric",month:"long",day:"numeric"}),o=t.toLocaleTimeString("en-US",{hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:!0});return`Current date and time: ${r} ${o} (${n})`}catch(t){return`Error getting time: ${t.message}`}}async function su(e){try{let t=String(e.expression||"");return t?/^[\d\+\-\*\/\(\)\.\s]+$/.test(t)?`Result: ${Function('"use strict"; return ('+t+")")()}`:"Error: expression contains unsafe characters":"Error: expression is required"}catch(t){return`Error evaluating expression: ${t.message}`}}function uu(e){Ae=e||{},_n=[]}function lu(){let e=_n;return _n=[],e}async function cu(e){try{let t=[],n=Ae.pageUrl??(typeof window<"u"?window.location.href:void 0),r=Ae.pageTitle??(typeof window<"u"?window.document.title:void 0);return t.push(`URL: ${n??"unknown from this context"}`),t.push(`Title: ${r??"unknown from this context"}`),typeof navigator<"u"&&(t.push(`User Agent: ${navigator.userAgent}`),t.push(`Online: ${navigator.onLine?"yes":"no"}`)),t.join(`
`)}catch(t){return`Error getting page context: ${t.message}`}}async function du(e){let t=Ae.apps??[];return t.length?["Windows you can open with open_app (use the id):",...t.map(n=>`- ${n.id} \u2014 ${n.title}: ${n.tagline}`)].join(`
`):"No windows are available to open from this surface."}async function pu(e){let t=String(e.app??e.name??e.id??"").trim().toLowerCase();if(!t)return"Error: which window? Pass app=<id>. Call list_apps to see the ids.";let n=Ae.apps??[];if(!n.length)return"No windows are available to open from this surface.";let r=n.find(o=>o.id.toLowerCase()===t)??n.find(o=>t.startsWith(o.id.toLowerCase())||o.id.toLowerCase().startsWith(t))??n.find(o=>o.title.toLowerCase()===t);return r?(_n.push({kind:"open",app:r.id}),`Opened the ${r.title} window (${r.tagline}).`):`There is no window called "${t}". Available: ${n.map(o=>o.id).join(", ")}.`}async function mu(e){let t=String(e.query??e.q??"").trim().toLowerCase(),n=Ae.apiBase,r=Ae.anonToken;if(!n)return"Knowledge is unavailable: no API origin was provided to this session.";if(!r)return"Knowledge is unavailable: no anon identity yet \u2014 the visitor has not been registered with the platform in this browser.";let o;try{o=await fetch(`${n}/api/sprite/me/knowledge`,{headers:{"X-Anon-Token":r,"Content-Type":"application/json"}})}catch(d){return`Knowledge is unavailable: could not reach ${n} (${d.message}).`}if(o.status===404)return"No sprite has been hatched in this browser yet, so there is no knowledge base to read. Open the Sprite window to hatch one.";if(!o.ok)return`Knowledge is unavailable: the server answered ${o.status}.`;let i;try{i=await o.json()}catch{return"Knowledge is unavailable: the server returned a 200 that was not JSON."}let a=Array.isArray(i)?i:i?.entries??i?.knowledge??[];if(!a.length)return"The sprite's knowledge base is empty \u2014 nothing has been taught to it yet.";let s=d=>String(d?.fact??d?.content??d?.text??d?.summary??JSON.stringify(d)),l=t?a.filter(d=>s(d).toLowerCase().includes(t)):a;return l.length?l.slice(0,8).map((d,u)=>`${u+1}. ${s(d).slice(0,300)}`).join(`
`):`The knowledge base has ${a.length} entr${a.length===1?"y":"ies"}, but none mention "${t}".`}async function hu(e){let t=String(e.query??e.q??"").trim(),n=Ae.apiBase;if(!t)return"Error: what should I search for? Pass query=<text>.";if(t.length>512)return"Error: that search is too long (max 512 characters).";if(!n)return"Web search is unavailable: no API origin was provided to this session.";let r;try{r=await fetch(`${n}/api/search/query`,{method:"POST",credentials:"include",headers:{"Content-Type":"application/json"},body:JSON.stringify({query:t})})}catch(a){return`Web search is unavailable: could not reach ${n} (${a.message}).`}if(r.status===401||r.status===403)return"Web search was refused by the server (it should be open to everyone). This looks like a misconfiguration rather than something you did.";if(r.status===429)return"Web search hit its hourly limit for this browser. Signing in raises the allowance; otherwise it resets within the hour.";if(!r.ok)return`Web search is unavailable: the server answered ${r.status}.`;let o;try{o=await r.json()}catch{return"Web search is unavailable: the server returned a 200 that was not JSON."}let i=o?.results??[];return i.length?i.slice(0,5).map((a,s)=>{let l=String(a?.title??a?.name??"untitled"),d=String(a?.snippet??a?.description??a?.content??"").slice(0,220),u=String(a?.url??a?.link??""),c=`${s+1}. ${l}`+(u?` \u2014 ${u}`:"");return d?c+`
   `+d:c}).join(`
`):`The web search for "${t}" returned no results.`}async function fu(e){let t=String(e.query??e.q??"").trim();if(!t)return"Error: what should I look up? Pass query=<text>.";let n=await si();if("error"in n)return`The Aitherium corpus is unavailable: it ${n.error}. Say you could not check the published material rather than answering from memory.`;let r=$t(t);if(!r.length)return`Error: "${t}" has no searchable words in it \u2014 try naming a product, feature or idea.`;let{ranked:o,miss:i}=Sr(n,r);if(i==="unknown-terms")return`Nothing in Aitherium's published material mentions "${t}". Say that we have not written about it, rather than answering from memory.`;if(i==="no-coverage")return`Nothing in Aitherium's published material covers "${t}". Say that we have not written about it, rather than answering from memory.`;let a=[],s=new Set;for(let{i:l}of o){let d=n.passages[l];if(s.has(d.d))continue;s.add(d.d);let u=n.docs[d.d],c=d.h?` (section: ${d.h})`:"",p=u.x?" [SUPERSEDED \u2014 later work replaced this; say so if you use it]":"";if(a.push(`[${a.length+1}] "${u.t}"${p}
    source: ${u.u}${u.d?`  (published ${u.d})`:""}${c}
    ${d.x}`),a.length>=5)break}return[`${a.length} passage(s) from Aitherium's published writing:`,"",a.join(`

`),"","Answer using ONLY these passages. After each claim, cite the source path of the passage it came from (for example: /blog/some-post). If they do not contain the answer, say so plainly instead of filling the gap."].join(`
`)}async function gu(e){let t=String(e.prompt??e.description??"").trim(),n=Ae.apiBase;if(!t)return"Error: what should I draw? Pass prompt=<description>.";if(t.length>512)return"Error: that image prompt is too long (max 512 characters).";if(Ae.localImageBase)try{let s=await fetch(`${Ae.localImageBase}/v1/generate`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({prompt:t,width:1024,height:1024})});if(s.ok){let l=await s.json(),d=Array.isArray(l?.images)?l.images[0]:null;if(d){let u=typeof d=="string"&&d.startsWith("data:")?d:`data:image/png;base64,${String(d)}`;return cn({dataUrl:u,alt:t.slice(0,200)}),`Generated an image for "${t.slice(0,60)}" on the user's own GPU (their local image backend). It is displayed to the user; do not describe it as if you can see it, and do not try to repeat it.`}}console.warn("[bonsai-tools] local image backend answered but unusably; falling to hosted")}catch(s){console.warn("[bonsai-tools] local image backend unreachable; falling to hosted:",s)}if(!n)return"Image generation is unavailable: no API origin was provided to this session.";let r;try{r=await fetch(`${n}/api/image/generate`,{method:"POST",credentials:"include",headers:{"Content-Type":"application/json"},body:JSON.stringify({prompt:t})})}catch(s){return`Image generation is unavailable: could not reach ${n} (${s.message}).`}if(r.status===401||r.status===403)return"Image generation needs you to be signed in. Unlike everything else here, it does NOT run on your machine \u2014 the model is a 7 GB diffusion model on Aitherium hardware, so it is tied to an account rather than being anonymous. Everything else the OS does stays on your GPU.";if(r.status===503)return"Image generation is switched off fleet-wide right now (there is a kill switch, and it is on). Nothing you did \u2014 try again later.";if(r.status===429)return"Image generation hit its rate limit. It is a minute of GPU time per picture, so the allowance is small; it resets shortly.";if(!r.ok)return`Image generation is unavailable: the server answered ${r.status}.`;let o;try{o=await r.json()}catch{return"Image generation returned an unreadable response."}let i=Array.isArray(o?.images)?o.images[0]:null;if(!i)return"Image generation returned no image.";let a=typeof i=="string"&&i.startsWith("data:")?i:`data:image/png;base64,${String(i)}`;return cn({dataUrl:a,alt:t.slice(0,200)}),`Generated an image for "${t.slice(0,60)}". It is displayed to the user; do not describe it as if you can see it, and do not try to repeat it.`}async function bu(e){let t=String(e.query??e.q??"").trim(),n=Ae.apiBase;if(!t)return"Error: what should I research? Pass query=<text>.";if(t.length>512)return"Error: that research question is too long (max 512 characters).";if(!n)return"Deep research is unavailable: no API origin was provided to this session.";let r=["quick","standard"].includes(String(e.depth))?String(e.depth):"standard",o;try{o=await fetch(`${n}/api/research`,{method:"POST",credentials:"include",headers:{"Content-Type":"application/json"},body:JSON.stringify({query:t,depth:r})})}catch(d){return`Deep research is unavailable: could not reach ${n} (${d.message}).`}if(o.status===429)return"Deep research hit its hourly limit for this browser. It is expensive to run, so the anonymous allowance is small; signing in raises it, and it resets within the hour. Try web_search for a lighter lookup.";if(o.status===401||o.status===403)return"Deep research was refused by the server (it should be open to everyone). This looks like a misconfiguration rather than something you did.";if(!o.ok)return`Deep research is unavailable: the server answered ${o.status}.`;let i;try{i=await o.json()}catch{return"Deep research is unavailable: the server returned a 200 that was not JSON."}let a=Array.isArray(i?.sources)?i.sources:[],s=String(i?.synthesis??"").trim();if(!s&&!a.length)return`Deep research for "${t}" came back with nothing \u2014 no pages worth reading.`;if(!a.length)return`Deep research for "${t}" produced a summary with NO sources attached, so none of it can be checked. Treat it as unverified and say so.`;let l=a.slice(0,5).map((d,u)=>{let c=String(d?.title||"untitled"),p=String(d?.url||""),h=String(d?.snippet||"").slice(0,240);return`[${u+1}] ${c}
    source: ${p}${h?`
    ${h}`:""}`}).join(`

`);return[s?`Summary of what the sources say:
${s}`:"Sources found:","",l,"","Cite the source URL beside any claim you take from this. These are third-party pages, not ours \u2014 if any of them describes a DIFFERENT project that happens to share a name with Aitherium, say so rather than repeating it. For anything about Aitherium or AitherOS itself, use search_aitherium instead; it is the authority."].join(`
`)}async function Li(){let[e,t]=await Promise.all([Promise.resolve().then(()=>(bi(),gi)),Promise.resolve().then(()=>(xi(),vi))]);return{local:e,graph:t}}async function wu(e){let t=String(e.fact??e.content??e.text??"").trim();if(!t)return"Error: what should I remember? Pass fact=<text>.";if(t.length>4e3)return"Error: that is too long to store as one memory (max 4000 characters). Break it into separate facts.";let n=String(e.title??"").trim()||t.slice(0,60),r;try{r=await Li()}catch(a){return`Memory is unavailable: the on-device store could not load (${a.message}).`}let o;try{o=await r.local.addKnowledge({kind:"fact",title:n,content:t})}catch(a){return`I could not save that: on-device storage refused the write (${a.message}). This is usually private browsing or blocked site storage.`}let i="";try{let a=await r.graph.ingestLocal(o);a.nodes&&(i=` Linked ${a.nodes} thing(s) and ${a.edges} connection(s) into your graph.`)}catch{i=" (Saved, but I could not connect it to anything yet.)"}return`Remembered: "${n}".${i} It is stored on this device and will still be here next time.`}async function yu(e){let t=String(e.query??e.q??e.about??"").trim(),n=Number(e.limit),r=Number.isFinite(n)&&n>=1?Math.min(Math.floor(n),8):4,o;try{o=await Li()}catch(s){return`Memory is unavailable: the on-device store could not load (${s.message}).`}let i;try{i=await o.local.listKnowledge(200)}catch(s){return`Memory is unavailable: could not read on-device storage (${s.message}).`}if(!i.length)return"Nothing has been stored in this device's memory yet. Use remember to add something.";try{let s=await o.graph.retrieveLocal(t,r);if(s.ids.length&&s.hops>0){let l=new Map(i.map(u=>[u.id,u])),d=s.ids.map(u=>l.get(u)).filter(Boolean);if(d.length)return[`From your on-device memory (${s.hops} hop(s) through the graph \u2014 these were reached by CONNECTION, not just word match):`,...d.map((u,c)=>`${c+1}. ${u.title}
   ${u.content.slice(0,400)}`)].join(`
`)}}catch{}let a=o.local.rankKnowledge(i,t,r);return a.length?["From your on-device memory (keyword match \u2014 nothing was connected to this yet):",...a.map((s,l)=>`${l+1}. ${s.title}
   ${String(s.content).slice(0,400)}`)].join(`
`):`Nothing in this device's memory matches "${t}". There are ${i.length} stored item(s), none about that.`}async function _u(e){let t=String(e.query??e.q??e.topic??"").trim();if(!t)return"Error: what should I look up? Pass query=<text>.";let n=await ui();if("error"in n)return`The Wikipedia reference is unavailable: it ${n.error}. Say you could not check rather than answering from memory.`;let r=$t(t);if(!r.length)return`Error: "${t}" has no searchable words in it \u2014 try naming a concept.`;let{ranked:o,miss:i}=Sr(n,r);if(i)return`The offline Wikipedia reference here covers ${n.docs.length} selected articles and none of them cover "${t}". It is not all of Wikipedia \u2014 say you do not have an article on it, and use web_search or deep_research if the question needs an answer.`;let a=[],s=new Set;for(let{i:l}of o){let d=n.passages[l];if(s.has(d.d))continue;s.add(d.d);let u=n.docs[d.d];if(a.push(`[${a.length+1}] ${u.t} (Wikipedia)
    source: ${u.u}
    ${d.x}`),a.length>=4)break}return[`${a.length} passage(s) from Wikipedia:`,"",a.join(`

`),"",`These are WIKIPEDIA passages, not Aitherium material \u2014 attribute them to Wikipedia and cite the article URL (text is ${n.license??"CC BY-SA"}). For anything about Aitherium or AitherOS itself use search_aitherium; this cannot speak for us.`].join(`
`)}function Ct(){return Object.values(Gt).map(e=>e.definition)}function Ai(e){return e===void 0||e<=300?400:e<=800?900:e<=2e3?1800:null}function Ti(e){return Math.ceil(JSON.stringify(e).length/4)}function Si(e){let t=ku.indexOf(e);return t===-1?Number.MAX_SAFE_INTEGER:t}function Ei(e){let t=Ai(e),n=Ct();if(t===null)return n;let r=[...n].sort((a,s)=>Si(a.name)-Si(s.name)),o=[],i=0;for(let a of r){let s=Ti(a);i+s>t||(o.push(a),i+=s)}return o}function vu(e){let t=Ei(e),n=new Set(t.map(r=>r.name));return{budget:Ai(e),sent:t.map(r=>r.name),dropped:Ct().map(r=>r.name).filter(r=>!n.has(r)),tokens:t.reduce((r,o)=>r+Ti(o),0)}}async function Er(e,t){let n=Gt[e];if(!n)return`Error: unknown tool "${e}"`;try{return await n.execute(t)}catch(r){return`Error executing tool: ${r.message}`}}var Ae,_n,Gt,ku,Br=G(()=>{"use strict";ar();oi();Lr();Lr();Ae={},_n=[];Gt={get_current_time:{definition:{name:"get_current_time",description:"Get the current date and time in the user's timezone",parameters:{type:"object",properties:{}}},execute:au},evaluate_math:{definition:{name:"evaluate_math",description:"Evaluate a mathematical expression and return the result",parameters:{type:"object",properties:{expression:{type:"string",description:'A mathematical expression to evaluate (e.g., "2 + 2", "sqrt(16)")'}},required:["expression"]}},execute:su},list_apps:{definition:{name:"list_apps",description:"List the windows/apps that can be opened here, with a short description of each. Call this before open_app if you are not sure of the id.",parameters:{type:"object",properties:{}}},execute:du},open_app:{definition:{name:"open_app",description:"Open one of this OS's windows for the user (for example the sprite, terminal, playground or setup window). Use list_apps to see the available ids.",parameters:{type:"object",properties:{app:{type:"string",description:'The window id to open, e.g. "sprite", "terminal", "playground".'}},required:["app"]}},execute:pu},web_search:{definition:{name:"web_search",description:"Search the live web for current information. Use this for anything you do not already know, or anything that may have changed recently. Works without an account.",parameters:{type:"object",properties:{query:{type:"string",description:"What to search the web for."}},required:["query"]}},execute:hu},search_knowledge:{definition:{name:"search_knowledge",description:"Search the visitor's own knowledge base \u2014 the facts they have taught their AitherSprite. Use this when asked what they have taught you, or what you know about a topic they have shared. Optional query filters the entries.",parameters:{type:"object",properties:{query:{type:"string",description:"Optional keyword to filter entries by. Omit to list everything."}}}},execute:mu},search_aitherium:{definition:{name:"search_aitherium",description:"Search everything Aitherium has PUBLISHED about itself \u2014 AitherOS, Aitherium, the products, the architecture, the mission, and how any of it works. Use this BEFORE answering any question about Aitherium or AitherOS, even if you think you know: it returns passages with the page they came from, so your answer can be checked. Prefer it over web_search for anything about us.",parameters:{type:"object",properties:{query:{type:"string",description:'What to look up, e.g. "how does AitherGraph work" or "why local AI".'}},required:["query"]}},execute:fu},search_wikipedia:{definition:{name:"search_wikipedia",description:'Look up general world knowledge \u2014 science, computing, history, concepts \u2014 in an offline Wikipedia reference that works with no network. Use it for "what is X" questions about things that are NOT Aitherium. For anything about Aitherium or AitherOS use search_aitherium instead; Wikipedia cannot speak for us.',parameters:{type:"object",properties:{query:{type:"string",description:"The concept or topic to look up."}},required:["query"]}},execute:_u},deep_research:{definition:{name:"deep_research",description:"Research a topic properly: searches the web, reads the pages, and returns a summary WITH its sources. Slower than web_search \u2014 use it when a question deserves a real answer rather than a list of links. Do NOT use it for questions about Aitherium or AitherOS; use search_aitherium for those, because the web has several unrelated projects with similar names.",parameters:{type:"object",properties:{query:{type:"string",description:"The question to research."},depth:{type:"string",description:'"quick" (fast, snippets) or "standard" (reads pages). Defaults to standard.'}},required:["query"]}},execute:bu},generate_image:{definition:{name:"generate_image",description:"Draw a picture from a description. UNLIKE everything else here this does NOT run on this machine \u2014 it runs on Aitherium hardware and needs the visitor to be signed in, so only use it when they actually asked for an image. It takes about a minute. The picture is shown to them directly; do not describe it as if you can see it.",parameters:{type:"object",properties:{prompt:{type:"string",description:"What the picture should show."}},required:["prompt"]}},execute:gu},remember:{definition:{name:"remember",description:"Store something durably on this device so you still know it in later conversations. Use it whenever the person tells you something about themselves, their preferences, their work, or anything they say to keep. It is saved locally and connected into their knowledge graph.",parameters:{type:"object",properties:{fact:{type:"string",description:"The thing to remember, in a full sentence."},title:{type:"string",description:"Optional short label for it."}},required:["fact"]}},execute:wu},recall:{definition:{name:"recall",description:"Look through everything stored on this device before answering anything personal or anything you were told earlier. It walks their knowledge graph, so it finds things CONNECTED to the question, not only exact word matches. Use it rather than guessing what you were told.",parameters:{type:"object",properties:{query:{type:"string",description:"What to look for."},limit:{type:"number",description:"How many items to return (1-8, default 4)."}},required:["query"]}},execute:yu},get_page_context:{definition:{name:"get_page_context",description:"Get information about the current page and browser environment",parameters:{type:"object",properties:{}}},execute:cu},...ri};ku=["get_current_time","open_app","search_aitherium","list_apps","evaluate_math","recall","remember","get_page_context","web_search","search_wikipedia","search_knowledge","generate_image","deep_research"]});function xu(e){let t="</tool_call>",n="",r=e;for(;;){let o=r.indexOf(t);if(o<0)return n+r;let i=r.slice(0,o),a=r.slice(o+t.length);if(i.includes("<tool_call>")){n+=r.slice(0,o+t.length),r=a;continue}let s=i.indexOf("{");if(s<0){n+=r.slice(0,o+t.length),r=a;continue}n+=`${i.slice(0,s)}<tool_call>${i.slice(s)}${t}`,r=a}}function Pi(e){return String(e).trim().replace(/\s+/g,"")}function Ii(e){let t=[];e=xu(e);let n=e,r=/<tool_call>([\s\S]*?)<\/tool_call>/g,o,i=[];for(;(o=r.exec(e))!==null;)i.push({full:o[0],content:o[1],index:o.index});if(i.length===0)return{toolCalls:[],remainingText:e};for(let a of i){let s=Su(a.content.trim());s&&t.push(s)}n=e;for(let a of i.reverse())n=n.slice(0,a.index)+n.slice(a.index+a.full.length);return n=n.trim(),{toolCalls:t,remainingText:n}}function Su(e){let t=e.trim();try{let r=JSON.parse(t);if(r.name&&r.arguments!==void 0)return{name:Pi(r.name),arguments:typeof r.arguments=="string"?Oi(r.arguments)||{}:r.arguments||{}}}catch{}let n=Lu(t);try{let r=JSON.parse(n);if(r.name&&r.arguments!==void 0)return{name:Pi(r.name),arguments:typeof r.arguments=="string"?Oi(r.arguments)||{}:r.arguments||{}}}catch(r){return console.warn("[tool-parser] failed to parse tool call:",e,r),null}return null}function Lu(e){let t=e;t=t.replace(/'([^']*)'/g,'"$1"'),t=t.replace(/(\{|,)\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*:/g,'$1"$2":'),t=t.replace(/,(\s*[}\]])/g,"$1");let n=(t.match(/{/g)||[]).length,r=(t.match(/}/g)||[]).length;n>r&&(t+="}".repeat(n-r));let o=(t.match(/\[/g)||[]).length,i=(t.match(/\]/g)||[]).length;return o>i&&(t+="]".repeat(o-i)),t}function Oi(e){try{return JSON.parse(e)}catch{return null}}var $i=G(()=>{"use strict"});var Ni={};oe(Ni,{extendMessagesWithToolResults:()=>Tu,getAvailableTools:()=>Eu,hasAvailableTools:()=>Bu,orchestrateToolCalls:()=>Au});async function Au(e,t,n=1/0){let r=[],{toolCalls:o,remainingText:i}=Ii(e);for(let a of o.slice(0,n)){let s=a.name,l={type:"tool_call",toolName:s,arguments:a.arguments};r.push(l),t?.(l);let d=await Er(s,a.arguments),u={type:"tool_result",toolName:s,result:d};r.push(u),t?.(u)}return{finalText:i,toolCalls:r}}function Tu(e,t){if(t.length===0)return e;let n=[...e],r=[],o=0;for(;o<t.length;){let a=t[o];a.type==="tool_call"&&a.toolName&&r.push({name:a.toolName,arguments:a.arguments||{}}),o++}let i=t.filter(a=>a.type==="tool_result").map(a=>`Tool ${a.toolName}: ${a.result}`).join(`

`);return i&&n.push({role:"tool",content:i}),n}function Eu(){return Ct()}function Bu(){return Object.keys(Gt).length>0}var Ri=G(()=>{"use strict";$i();Br()});function Yi(e){return e===408||e===429||e>=500}var Tn=e=>new Promise(t=>setTimeout(t,e)),Bt=2e4;function En(e){return async(t,n)=>{let r=((n-t+1)/1048576).toFixed(1),o="";for(let i=1;i<=3;i++){let a=new AbortController,s=!1,l=()=>setTimeout(()=>{s=!0,a.abort()},Bt),d=l(),u=()=>{clearTimeout(d),d=l()};try{let c;try{c=await fetch(e,{headers:{Range:`bytes=${t}-${n}`},cache:"force-cache",signal:a.signal})}catch(p){if(o=p instanceof Error?p.message:String(p),s&&(o=`stalled (no response for ${Bt/1e3}s)`),i<3){await Tn(250*2**(i-1));continue}let h=typeof navigator<"u"&&navigator.onLine===!1;throw new Error(`bonsai-gguf: fetch failed for ${r} MB range ${t}-${n} of ${e} after 3 attempts${h?" (browser reports OFFLINE)":""}. `+(s?`The server accepted the connection but never answered (stalled ${Bt/1e3}s). `:"")+`If the screen also flickered, the GPU driver reset and took this request with it \u2014 that is a GPU fault, not a network one. Last error: ${o}`)}if(c.status!==206&&c.status!==200){if(o=`HTTP ${c.status}`,Yi(c.status)&&i<3){await Tn(250*2**(i-1));continue}throw new Error(`bonsai-gguf: range GET ${t}-${n} (${r} MB) of ${e} returned ${c.status}`)}try{let p=c.body;if(!p)return new Uint8Array(await c.arrayBuffer());let h=p.getReader(),m=[],g=0;for(;;){let{done:_,value:y}=await h.read();if(_)break;y&&(u(),m.push(y),g+=y.byteLength)}let f=new Uint8Array(g),b=0;for(let _ of m)f.set(_,b),b+=_.byteLength;return f}catch(p){if(s){if(o=`stalled mid-body (no progress for ${Bt/1e3}s)`,i<3){await Tn(250*2**(i-1));continue}throw new Error(`bonsai-gguf: range GET ${t}-${n} (${r} MB) of ${e} stalled mid-body (no progress for ${Bt/1e3}s) after 3 attempts. Last error: ${o}`)}throw new Error(`bonsai-gguf: reading ${r} MB range body failed (device out of memory?): ${p instanceof Error?p.message:String(p)}`)}}finally{clearTimeout(d)}}throw new Error(`bonsai-gguf: range ${t}-${n} exhausted retries: ${o}`)}}function Wr(e){let t=e.filter(Boolean);if(t.length===0)throw new Error("bonsai-gguf: mirroredRangeFetcher needs at least one URL");if(t.length===1)return En(t[0]);let n=t.map(r=>En(r));return async(r,o)=>{let i=[];for(let a=0;a<n.length;a++)try{return await n[a](r,o)}catch(s){i.push(`${t[a]}: ${s instanceof Error?s.message:String(s)}`),a+1<n.length&&console.warn(`[bonsai-gguf] mirror ${a+1}/${n.length} failed, trying next`)}throw new Error(`bonsai-gguf: all ${n.length} mirrors failed for range ${r}-${o}:
`+i.map((a,s)=>`  [${s+1}] ${a}`).join(`
`))}}var Kt=class{constructor(t){this.filled=0;this.cursor=0;this.url=t.url,this.fetchRange=t.fetchRange??En(t.url),this.contentLength=t.contentLength,this.initialWindow=t.initialWindow??1<<20,this.buf=new Uint8Array(0)}get position(){return this.cursor}async ensure(t){if(t<=this.filled)return;let n=Math.max(t,this.filled+this.initialWindow);this.contentLength!==void 0&&(n=Math.min(n,this.contentLength));let r=await this.fetchRange(this.filled,n-1),o=new Uint8Array(this.filled+r.length);if(o.set(this.buf.subarray(0,this.filled),0),o.set(r,this.filled),this.buf=o,this.filled+=r.length,this.filled<t)throw new Error(`bonsai-gguf: underfilled window (have ${this.filled}, need ${t}) \u2014 server may not support ranges`)}async view(t){return await this.ensure(this.cursor+t),new DataView(this.buf.buffer,this.buf.byteOffset+this.cursor,t)}async u8(){let t=(await this.view(1)).getUint8(0);return this.cursor+=1,t}async u32(){let t=(await this.view(4)).getUint32(0,!0);return this.cursor+=4,t}async i32(){let t=(await this.view(4)).getInt32(0,!0);return this.cursor+=4,t}async f32(){let t=(await this.view(4)).getFloat32(0,!0);return this.cursor+=4,t}async f64(){let t=(await this.view(8)).getFloat64(0,!0);return this.cursor+=8,t}async u16(){let t=(await this.view(2)).getUint16(0,!0);return this.cursor+=2,t}async i16(){let t=(await this.view(2)).getInt16(0,!0);return this.cursor+=2,t}async i8(){let t=(await this.view(1)).getInt8(0);return this.cursor+=1,t}async u64(){let t=await this.view(8),n=t.getUint32(0,!0),r=t.getUint32(4,!0);this.cursor+=8;let o=r*4294967296+n;if(!Number.isSafeInteger(o))throw new Error(`bonsai-gguf: u64 ${o} exceeds MAX_SAFE_INTEGER`);return o}async i64(){return this.u64()}async string(){let t=await this.u64();await this.ensure(this.cursor+t);let n=this.buf.subarray(this.cursor,this.cursor+t);return this.cursor+=t,new TextDecoder("utf-8").decode(n)}seek(t){this.cursor=t}async bytes(t,n){return await this.ensure(t+n),this.buf.slice(t,t+n)}};var Xi="bonsai-weights",wt="ranges";function Vi(){return new Promise((e,t)=>{let n=indexedDB.open(Xi,1);n.onupgradeneeded=()=>{let r=n.result;r.objectStoreNames.contains(wt)||r.createObjectStore(wt)},n.onsuccess=()=>e(n.result),n.onerror=()=>t(n.error)})}function Ji(e,t){return new Promise((n,r)=>{let i=e.transaction(wt,"readonly").objectStore(wt).get(t);i.onsuccess=()=>{let a=i.result;n(a===void 0?void 0:a instanceof Uint8Array?a:new Uint8Array(a))},i.onerror=()=>r(i.error)})}function Zi(e,t,n){return new Promise((r,o)=>{let i=e.transaction(wt,"readwrite"),a=n.slice();i.objectStore(wt).put(a.buffer,t),i.oncomplete=()=>r(),i.onerror=()=>o(i.error),i.onabort=()=>o(i.error)})}function jr(e,t,n){let r=null,o=()=>{if(!r){try{navigator.storage?.persist?.()}catch{}r=Vi().catch(()=>null)}return r};return async(i,a)=>{let s=`${e}#${i}-${a}`,l=await o();if(l)try{let u=await Ji(l,s);if(u)return n?.({bytes:u.byteLength,fromCache:!0}),u}catch{}let d=await t(i,a);return n?.({bytes:d.byteLength,fromCache:!1}),l&&Zi(l,s,d).catch(()=>{}),d}}yt();var ta=1179993927;function Xr(e,t){return e+(t-e%t)%t}async function Yr(e,t){switch(t){case 0:return e.u8();case 1:return e.i8();case 2:return e.u16();case 3:return e.i16();case 4:return e.u32();case 5:return e.i32();case 6:return e.f32();case 7:return await e.u8()!==0;case 8:return e.string();case 10:return e.u64();case 11:return e.i64();case 12:return e.f64();default:throw new Error(`bonsai-gguf: cannot read scalar of value-type ${t}`)}}async function na(e,t){if(t===9){let n=await e.u32(),r=await e.u64();if(n===9)throw new Error("bonsai-gguf: nested arrays are not permitted by the spec");let o=new Array(r);for(let i=0;i<r;i++)o[i]=await Yr(e,n);return o}return Yr(e,t)}async function Vr(e){let t=await e.u32();if(t!==ta)throw new Error(`bonsai-gguf: bad magic 0x${t.toString(16)} (expected 0x46554747)`);let n=await e.u32();if(n!==3)throw new Error(`bonsai-gguf: unsupported GGUF version ${n} (need 3)`);let r=await e.u64(),o=await e.u64(),i={version:n,tensorCount:r,metadataKvCount:o},a=new Map;for(let u=0;u<o;u++){let c=await e.string(),p=await e.u32(),h=await na(e,p);a.set(c,h)}let s=oa(a,"general.alignment",32),l=[];for(let u=0;u<r;u++){let c=await e.string(),p=await e.u32(),h=new Array(p);for(let _=0;_<p;_++)h[_]=await e.u64();let m=await e.u32(),g=await e.u64(),f=h.reduce((_,y)=>_*y,1);Pt(m);let b=Hr(m,f);l.push({name:c,dims:h,type:m,relOffset:g,nElements:f,nBytes:b})}let d=Xr(e.position,s);return ra(l,s),{header:i,kv:a,tensors:l,tensorDataBase:d,alignment:s}}function ra(e,t){if(e.length<2)return;let n=[...e].sort((r,o)=>r.relOffset-o.relOffset);for(let r=0;r<n.length-1;r++){let o=n[r],i=n[r+1].relOffset-o.relOffset,a=Xr(o.nBytes,t);if(i===a)continue;let s=Pt(o.type),l=o.nBytes>0?i/o.nBytes:0;throw new Error(`bonsai-gguf: tensor '${o.name}' (type ${o.type} = ${s.name}) occupies ${i} bytes in the file but this build computes ${o.nBytes} (aligned ${a}) from ${s.blockSize} weights/${s.typeSize} bytes per block \u2014 a factor of ${l.toFixed(4)}. The declared type id does not match the file's actual block geometry, so every read of this tensor would be at the wrong stride and would produce plausible-looking WRONG values rather than an error. If this is a '*_g64' ternary file, it uses group 64 under the same type id 42 and is NOT loadable by this runtime \u2014 use the group-128 '*-Q2_0.gguf' build.`)}}function oa(e,t,n){let r=e.get(t);return typeof r=="number"?r:typeof r=="bigint"?Number(r):n}var Wt=class{constructor(t){this.kv=t}raw(t){return this.kv.get(t)}str(t,n){let r=this.kv.get(t);if(typeof r=="string")return r;if(n!==void 0)return n;throw new Error(`bonsai-gguf: missing string key '${t}'`)}num(t,n){let r=this.kv.get(t);if(typeof r=="number")return r;if(typeof r=="bigint")return Number(r);if(n!==void 0)return n;throw new Error(`bonsai-gguf: missing numeric key '${t}'`)}numOpt(t){let n=this.kv.get(t);if(typeof n=="number")return n;if(typeof n=="bigint")return Number(n)}strArray(t){let n=this.kv.get(t);if(Array.isArray(n))return n;throw new Error(`bonsai-gguf: missing string-array key '${t}'`)}numArray(t){let n=this.kv.get(t);if(Array.isArray(n))return n.map(Number);throw new Error(`bonsai-gguf: missing numeric-array key '${t}'`)}get arch(){let t=this.str("general.architecture");return t==="dspark"?"qwen35":t}a(t){return`${this.arch}.${t}`}resolveArchConfig(){return{arch:this.arch,contextLength:this.num(this.a("context_length")),embeddingLength:this.num(this.a("embedding_length")),blockCount:this.num(this.a("block_count")),feedForwardLength:this.num(this.a("feed_forward_length")),headCount:this.num(this.a("attention.head_count")),headCountKv:this.num(this.a("attention.head_count_kv")),keyLength:this.numOpt(this.a("attention.key_length")),valueLength:this.numOpt(this.a("attention.value_length")),rmsEps:this.num(this.a("attention.layer_norm_rms_epsilon"),1e-6),ropeDimensionCount:this.numOpt(this.a("rope.dimension_count")),ropeDimensionSections:(()=>{let n=this.kv.get(this.a("rope.dimension_sections"));return Array.isArray(n)?n.map(Number):[]})(),ropeFreqBase:this.numOpt(this.a("rope.freq_base"))??1e4,ropeScalingType:(()=>{let n=this.kv.get(this.a("rope.scaling.type"));return typeof n=="string"?n:"none"})(),ropeScalingFactor:this.numOpt(this.a("rope.scaling.factor")),ssmConvKernel:this.numOpt(this.a("ssm.conv_kernel")),ssmInnerSize:this.numOpt(this.a("ssm.inner_size")),ssmStateSize:this.numOpt(this.a("ssm.state_size")),ssmGroupCount:this.numOpt(this.a("ssm.group_count")),ssmTimeStepRank:this.numOpt(this.a("ssm.time_step_rank")),fullAttentionInterval:this.numOpt(this.a("full_attention_interval"))}}resolveTokenizer(){return{model:this.str("tokenizer.ggml.model","gpt2"),tokens:this.strArray("tokenizer.ggml.tokens"),merges:(()=>{let t=this.kv.get("tokenizer.ggml.merges");return Array.isArray(t)?t:[]})(),tokenType:(()=>{let t=this.kv.get("tokenizer.ggml.token_type");return Array.isArray(t)?t.map(Number):[]})(),bosTokenId:this.numOpt("tokenizer.ggml.bos_token_id"),eosTokenId:this.numOpt("tokenizer.ggml.eos_token_id")}}};var jt=class{constructor(t){this.byName=new Map;this.ordered=[];this.tensorDataBase=t.tensorDataBase;for(let n of t.tensors){let r=this.toEntry(n,t.tensorDataBase);this.byName.set(r.name,r),this.ordered.push(r)}this.ordered.sort((n,r)=>n.absStart-r.absStart)}toEntry(t,n){let r=n+t.relOffset;return{name:t.name,type:t.type,dims:t.dims,absStart:r,nBytes:t.nBytes,absEnd:r+t.nBytes}}get(t){let n=this.byName.get(t);if(!n)throw new Error(`bonsai-tensors: no tensor named '${t}'`);return n}has(t){return this.byName.has(t)}withPrefix(t){return this.ordered.filter(n=>n.name.startsWith(t))}coalesce(t,n=1<<20,r=64<<20){let o=[...t].sort((a,s)=>a.absStart-s.absStart),i=[];for(let a of o){let s=i[i.length-1];s&&a.absStart-s.absEnd<=n&&a.absEnd-s.absStart<=r?(s.absEnd=Math.max(s.absEnd,a.absEnd),s.nBytes=s.absEnd-s.absStart,s.members.push(a)):i.push({absStart:a.absStart,absEnd:a.absEnd,nBytes:a.nBytes,members:[a]})}return i}coalesceBlock(t){return this.coalesce(this.withPrefix(`blk.${t}.`))}};function ia(e){let t=e.ssmTimeStepRank??0,n=e.ssmGroupCount??0,r=e.ssmStateSize??0,o=e.ssmInnerSize??t*r,i=n*r,a=n*r,s=i+a+o,l=e.ssmConvKernel??0;if(t<=0||n<=0||r<=0||l<=0)throw new Error(`bonsai-config: '${e.arch}' has no DeltaNet layers \u2014 this in-browser runtime only runs the qwen35 hybrid (Bonsai-27B). Dense sizes run on a local node or the hosted lane instead. (numVHeads=${t}, numKHeads=${n}, headDim=${r}, convKernel=${l})`);if(t%n!==0)throw new Error(`bonsai-config: numVHeads ${t} not divisible by numKHeads ${n}`);if(o!==t*r)throw new Error(`bonsai-config: ssm.inner_size ${o} != numVHeads*headDim ${t*r}`);return{numVHeads:t,numKHeads:n,headDim:r,qDim:i,kDim:a,vDim:o,convDim:s,convKernel:l,vPerKHead:t/n}}function aa(e,t){let n=t.blockCount,r=t.keyLength&&t.keyLength>0?t.keyLength:t.embeddingLength/t.headCount,o=t.headCount*r,i=o*2,a=[];for(let s=0;s<n;s++){let l=`blk.${s}.`;if(e.ordered.some(m=>m.name.startsWith(l)&&m.name.includes("ssm"))){a.push("linear-attn");continue}if(!(e.has(`${l}attn_k.weight`)||e.has(`${l}attn_v.weight`)||e.ordered.some(m=>m.name.startsWith(l)&&/attn_(k|v)\b/.test(m.name))))throw new Error(`bonsai-config: block ${s} has neither ssm_* nor attn_k/v tensors \u2014 cannot classify layer`);let c=`${l}attn_q.weight`;if(!e.has(c))throw new Error(`bonsai-config: block ${s} has attn_k/v but no '${c}' \u2014 cannot determine whether its attention is gated (qwen35) or plain (qwen3)`);let p=e.get(c).dims,h=p.length>=2?p[p.length-1]:p[0];if(h===i)a.push("full-attn");else if(h===o)a.push("dense-attn");else throw new Error(`bonsai-config: block ${s} '${c}' has output width ${h}, which matches neither plain attention (nHeads*headDim = ${o}) nor gated attention (2*nHeads*headDim = ${i}). headCount=${t.headCount}, headDim=${r} (key_length=${t.keyLength??"absent"}, embedding_length=${t.embeddingLength}). Refusing to guess \u2014 the wrong choice produces fluent garbage, not an error.`)}return a}function sa(e,t){let n=[];for(let r=0;r<t;r++){let o=`blk.${r}.post_attention_norm.weight`,i=`blk.${r}.ffn_norm.weight`;if(e.has(o))n.push(o);else if(e.has(i))n.push(i);else throw new Error(`bonsai-config: block ${r} has neither '${o}' nor '${i}' \u2014 cannot locate the pre-FFN norm`)}return n}var st=256;function ua(e){let t=e.keyLength&&e.keyLength>0?e.keyLength:e.embeddingLength/e.headCount,n;return e.ssmInnerSize!==void 0&&e.headCount>0&&e.ssmInnerSize%e.headCount===0&&(n=e.ssmInnerSize/e.headCount),{headDim:t,deltaNetDv:n}}function Jr(e){let{headDim:t,deltaNetDv:n}=ua(e);if(!Number.isInteger(t)||t<=0)throw new Error(`bonsai-config: head_dim (embedding_length ${e.embeddingLength} / head_count ${e.headCount}) = ${t} is not a positive integer \u2014 cannot size attention kernels`);if(t>st)throw new Error(`bonsai-config: head_dim ${t} exceeds the WGSL fixed array bound ${st} (softmax_attn.wgsl acc[${st}]) \u2014 refusing to load; the kernel would read out of bounds on the GPU`);if(n!==void 0&&n>st)throw new Error(`bonsai-config: DeltaNet d_v ${n} (ssm.inner_size ${e.ssmInnerSize} / head_count ${e.headCount}) exceeds the WGSL fixed array bound ${st} (deltanet.wgsl err/o[${st}]) \u2014 refusing to load`);let r=n!==void 0?`, DeltaNet d_v=${n}`:"";return{message:`head_dim=${t}${r} (<= ${st})`}}function Zr(e,t){let n=aa(t,e),r=[],o=[];n.forEach((s,l)=>(s==="linear-attn"?o:r).push(l));let i=o.length>0?ia(e):void 0,a=sa(t,e.blockCount);return{...e,layerKinds:n,fullAttnLayers:r,linearAttnLayers:o,deltaNet:i,ffnNormNames:a}}function eo(e){let t=e.fullAttnLayers.length+e.linearAttnLayers.length;if(t!==e.blockCount)return{ok:!1,message:`layer kinds (${t}) != blockCount (${e.blockCount})`};if(e.linearAttnLayers.length===0){let n=e.layerKinds.filter(r=>r==="dense-attn").length;return{ok:!0,message:`dense: ${n} plain-attn / ${e.blockCount-n} gated-attn, no DeltaNet`}}return e.blockCount===64&&e.fullAttnLayers.length!==16&&console.warn(`bonsai-config: Bonsai-27B expected 16 full-attn layers (64 blocks), got ${e.fullAttnLayers.length}. This may be a model variant; loading anyway.`),{ok:!0,message:`${e.fullAttnLayers.length} full-attn / ${e.linearAttnLayers.length} linear-attn`}}function la(){let e=[];for(let o=33;o<=126;o++)e.push(o);for(let o=161;o<=172;o++)e.push(o);for(let o=174;o<=255;o++)e.push(o);let t=[...e],n=0;for(let o=0;o<256;o++)e.includes(o)||(e.push(o),t.push(256+n),n++);let r=new Map;for(let o=0;o<e.length;o++)r.set(e[o],String.fromCodePoint(t[o]));return r}var ca=3,da=4;function oo(e,t,n=[]){let r=new Map;e.forEach((u,c)=>r.set(u,c));let o=new Map;t.forEach((u,c)=>o.set(u,c));let i=la(),a=new Map;i.forEach((u,c)=>a.set(u,c));let s=[],l=n.length===e.length;e.forEach((u,c)=>{(l?n[c]===ca||n[c]===da:u.length>=5&&u.startsWith("<|")&&u.endsWith("|>"))&&s.push([u,c])}),s.sort((u,c)=>c[0].length-u[0].length);let d=new Map(s);return{vocab:r,idToToken:e,mergeRank:o,byteEncoder:i,byteDecoder:a,specialTokens:d}}function pa(e,t){if(e.length<2)return e;let n=e;for(;;){let r=1/0,o=-1;for(let i=0;i<n.length-1;i++){let a=t.get(`${n[i]} ${n[i+1]}`);a!==void 0&&a<r&&(r=a,o=i)}if(o===-1)break;n=[...n.slice(0,o),n[o]+n[o+1],...n.slice(o+2)]}return n}var to=/'s|'t|'re|'ve|'m|'ll|'d| ?\p{L}+| ?\p{N}+| ?[^\s\p{L}\p{N}]+|\s+(?!\S)|\s+/gu,no=new WeakMap;function ma(e){let t=no.get(e);if(t===void 0){if(e.specialTokens.size===0)t=null;else{let n=[...e.specialTokens.keys()].map(r=>r.replace(/[.*+?^${}()|[\]\\]/g,"\\$&")).join("|");t=new RegExp(n,"g")}no.set(e,t)}return t}function ro(e,t,n){let r=e;for(;r.length>0;){to.lastIndex=0;let o=to.exec(r);if(!o)break;let i=o[0],s=new TextEncoder().encode(i),l=Array.from(s,u=>t.byteEncoder.get(u)),d=pa(l,t.mergeRank);for(let u of d){let c=t.vocab.get(u);if(c!==void 0)n.push(c);else for(let p of u){let h=t.vocab.get(p);h!==void 0&&n.push(h)}}r=r.slice(i.length)}}function io(e,t){let n=[],r=ma(t),o=0;if(r){r.lastIndex=0;let i;for(;(i=r.exec(e))!==null;)i.index>o&&ro(e.slice(o,i.index),t,n),n.push(t.specialTokens.get(i[0])),o=i.index+i[0].length}return o<e.length&&ro(e.slice(o),t,n),n}function ao(e,t){let n="";for(let o of e){let i=t.idToToken[o];i!==void 0&&(n+=i)}let r=[];for(let o of n){let i=t.byteDecoder.get(o);i!==void 0&&r.push(i)}return new TextDecoder("utf-8",{fatal:!1}).decode(new Uint8Array(r))}function so(e,t=!0,n){let r="";if(n&&n.length>0){r+=`<|im_start|>system
`,e[0]?.role==="system"&&(r+=e[0].content+`

`),r+=`# Tools

You may call one or more functions to assist with the user query.

`,r+=`You are provided with function signatures within <tools></tools> XML tags:
`,r+="<tools>";for(let i of n)r+=`
`+JSON.stringify(i);r+=`
</tools>

`,r+=`For each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:
`,r+=`<tool_call>
`,r+=`{"name": <function-name>, "arguments": <args-json-object>}
`,r+="</tool_call>",r+=`<|im_end|>
`}else e[0]?.role==="system"&&(r+=`<|im_start|>system
${e[0].content}<|im_end|>
`);let o=n&&n.length>0&&e[0]?.role==="system"||!n&&e[0]?.role==="system"?1:0;for(let i=o;i<e.length;i++){let a=e[i];if(a.role==="user")r+=`<|im_start|>user
${a.content}<|im_end|>
`;else if(a.role==="assistant"){let s=a.content,l=a.reasoning_content||"";if(l?r+=`<|im_start|>assistant
<think>
${l.trim()}
</think>

`:r+=`<|im_start|>assistant
`,s&&(r+=s),a.tool_calls&&a.tool_calls.length>0)for(let d of a.tool_calls){s&&(r+=`
`);let u=d.function||d;r+=`<tool_call>
`,r+=JSON.stringify({name:u.name,arguments:typeof u.arguments=="string"?JSON.parse(u.arguments):u.arguments}),r+=`
</tool_call>`}r+=`<|im_end|>
`}else a.role==="tool"&&(r+=`<|im_start|>user
<tool_response>
${a.content}
</tool_response><|im_end|>
`)}return t&&(r+=`<|im_start|>assistant
<think>

</think>

`),r}var Qt=class{constructor(t){this.tables=oo(t.tokens,t.merges,t.tokenType),this.bosTokenId=t.bosTokenId,this.eosTokenId=t.eosTokenId,this.thinkStartId=this.tables.specialTokens.get("<think>"),this.thinkEndId=this.tables.specialTokens.get("</think>");let n=new Set;t.eosTokenId!==void 0&&n.add(t.eosTokenId);for(let r of["<|im_end|>","<|endoftext|>"]){let o=this.tables.specialTokens.get(r);o!==void 0&&n.add(o)}this.stopIds=n}get vocabSize(){return this.tables.idToToken.length}encode(t){return io(t,this.tables)}decode(t){return ao(t,this.tables)}encodeChat(t,n){return this.encode(so(t,!0,n))}isStop(t){return this.stopIds.has(t)}isEos(t){return this.eosTokenId!==void 0&&t===this.eosTokenId}};yt();_t();var ha=134217728;function fa(e,t){return t>e.limits.maxStorageBufferBindingSize}async function Pn(e,t,n){let r=await t(n.absStart,n.absEnd-1),o=[];for(let i of n.members){let a=i.absStart-n.absStart,s=r.subarray(a,a+i.nBytes);if(fa(e,i.nBytes)){let u=e.limits.maxStorageBufferBindingSize,c=u===ha?" \u2014 this is the WebGPU DEFAULT limit, so the device was almost certainly created without requiredLimits; mirror adapter.limits in requestDevice()":" \u2014 this adapter genuinely caps here; a chunked upload path is required";throw new Error(`bonsai-upload: tensor '${i.name}' (${i.nBytes} B) exceeds maxStorageBufferBindingSize (${u})${c}`)}let l=i.type===ga?wa(s):i.type===ba?ya(s):_a(s),d=e.createBuffer({size:l.byteLength,usage:D.STORAGE|D.COPY_DST|D.COPY_SRC,label:i.name});e.queue.writeBuffer(d,0,l),o.push({entry:i,buffer:d})}return o}var ga=41,ba=42,zt=18,lo=20,Ht=34,co=36;function wa(e){let t=Math.floor(e.length/zt),n=new Uint8Array(t*lo);for(let r=0;r<t;r++)n.set(e.subarray(r*zt,r*zt+zt),r*lo);return n}function ya(e){let t=Math.floor(e.length/Ht),n=new Uint8Array(t*co);for(let r=0;r<t;r++)n.set(e.subarray(r*Ht,r*Ht+Ht),r*co);return n}function _a(e){let t=ka(e.length,4);if(t===e.length)return e;let n=new Uint8Array(t);return n.set(e),n}function ka(e,t){return e+(t-e%t)%t}var Yt=class{constructor(t,n,r){this.device=t;this.registry=n;this.fetchRange=r;this.buffers=new Map;this.loadedLayers=new Set;this.inflight=new Map}has(t){return this.buffers.has(t)}get(t){let n=this.buffers.get(t);if(!n)throw new Error(`bonsai-weights: '${t}' not resident (load its layer first)`);return n}typeOf(t){return this.registry.get(t).type}weightQuantType(){if(this.blockQuantType!==void 0)return this.blockQuantType;let t=o=>o===0||o===1,n=this.registry.ordered.filter(o=>o.name.startsWith("blk.")&&!t(o.type));if(n.length===0)throw new Error("bonsai-weights: no quantized 'blk.*' weight tensors in the registry \u2014 cannot determine the model's weight quant type");let r=new Map;for(let o of n)r.has(o.type)||r.set(o.type,o.name);for(let[o,i]of r)if(o!==41&&o!==42)throw new Error(`bonsai-weights: block tensor '${i}' has unsupported quant type ${o} (supported: Q1_0=41, Q2_0=42)`);if(r.size>1){let o=[...r].map(([i,a])=>`${i} (e.g. '${a}')`).join(", ");throw new Error(`bonsai-weights: decoder blocks mix quant types \u2014 ${o}. The block projections dispatch once per context, so a mixed file would silently run some layers through the wrong kernel and emit fluent garbage. Use projectQuantized per tensor to support this.`)}return this.blockQuantType=n[0].type,this.blockQuantType}register(t){for(let n of t)this.buffers.set(n.entry.name,n.buffer)}async loadGlobals(t){let n=t.filter(r=>this.registry.has(r)).map(r=>this.registry.get(r));for(let r of this.registry.coalesce(n))this.register(await Pn(this.device,this.fetchRange,r))}ensureLayer(t){if(this.loadedLayers.has(t))return Promise.resolve();let n=this.inflight.get(t);if(n)return n;let r=this.loadLayer(t).finally(()=>this.inflight.delete(t));return this.inflight.set(t,r),r}async loadLayer(t){for(let n of this.registry.coalesceBlock(t))this.register(await Pn(this.device,this.fetchRange,n));this.loadedLayers.add(t)}prefetchLayer(t){t<0||this.loadedLayers.has(t)||this.registry.coalesceBlock(t).length!==0&&this.ensureLayer(t).catch(()=>{})}get residentLayerCount(){return this.loadedLayers.size}evictLayer(t,n){this.inflight.delete(t);for(let r of n){let o=this.buffers.get(r);o&&(o.destroy(),this.buffers.delete(r))}this.loadedLayers.delete(t)}};var va=["quantize_q8_0","q1_0_dequant","q1_0_q8_0_matmul","q2_0_dequant","q2_0_q8_0_matmul","kv_quant_4bit","rmsnorm","rope_imrope","softmax_attn","softmax_attn_batched","causal_conv1d","deltanet","deltanet_gate","deltanet_seq","swiglu","sampling","logit_topk","vae_ops","elementwise","elementwise_inplace","image_ops"],Xt=class{constructor(t,n){this.device=t;this.sources=n;this.cache=new Map}get(t,n="main"){let r=n==="main"?t:`${t}:${n}`,o=this.cache.get(r);if(o)return o;let i=this.sources[t];if(!i)throw new Error(`bonsai-pipelines: no WGSL source registered for '${t}'`);let a=this.device.createShaderModule({code:i,label:t}),s=this.device.createComputePipeline({label:r,layout:"auto",compute:{module:a,entryPoint:n}});return this.cache.set(r,s),s}warmAll(){for(let t of va)if(this.sources[t]){if(t==="logit_topk"){this.get(t,"hist_main"),this.get(t,"gather_main");continue}if(t==="vae_ops"){this.get(t,"conv2d_main"),this.get(t,"groupnorm_main"),this.get(t,"upsample_nearest_main");continue}this.get(t)}}};var On=class{constructor(t){this.deps=t}async load(t){let n=t.mirrorUrls?.length?t.mirrorUrls:[t.modelUrl],r=this.deps.fetchRange??Wr(n),o=this.deps.fetchRange?r:jr(t.modelUrl,r),i=t.onProgress??(()=>{});i({phase:"parse",percent:2,detail:"range-fetching header + KV"});let a=new Kt({url:t.modelUrl,fetchRange:o}),s=await Vr(a),l=new Wt(s.kv);i({phase:"config",percent:30,detail:`arch=${l.arch}`});let d=new jt(s),u=l.resolveArchConfig(),c=Zr(u,d),p=eo(c),h=Jr(c);console.log(`bonsai: kernel dims OK \u2014 ${h.message}`),i({phase:"tokenizer",percent:45,detail:"building BPE tables"});let m=new Qt(l.resolveTokenizer());i({phase:"pipelines",percent:60,detail:"compiling WGSL"});let g=new Xt(this.deps.device,this.deps.kernelSources);g.warmAll(),i({phase:"globals",percent:75,detail:"uploading embeddings + LM head + norms"});let f=new Yt(this.deps.device,d,o),b=["token_embd.weight","output_norm.weight"];await f.loadGlobals(b);for(let v of b)if(!f.has(v))throw new Error(`bonsai-runtime: required tensor '${v}' was not found in the GGUF file. The model file may be corrupted or incomplete \u2014 try clearing your browser cache and reloading, or switch to a different model size.`);let _=["output.weight"];try{await f.loadGlobals(_)}catch(v){console.warn(`bonsai-runtime: optional globals not loaded: ${v.message}`)}i({phase:"ready",percent:100});let y={device:this.deps.device,parsed:s,meta:l,registry:d,config:c,tokenizer:m,pipelines:g,weights:f,scheduleOk:p.ok,scheduleMessage:p.message};return this.model=y,y}get loaded(){return this.model}async warm(t){let n=this.model;if(!n)return 0;let r=n.config.blockCount,o=Math.max(1,Math.min(t?.concurrency??3,r)),i=0,a=async()=>{for(;;){if(t?.cancelled?.())return;let s=i++;if(s>=r)return;try{await n.weights.ensureLayer(s)}catch{}}};return await Promise.all(Array.from({length:o},a)),n.weights.residentLayerCount}};function mo(e){return new On(e)}var kt="<tool_call>",Vt="</tool_call>";function ho(e,t){if(!t)return e;let n="",r=0;for(;;){let s=e.indexOf(Vt,r);if(s===-1)break;let l=fo(e,r,s);n+=e.slice(r,l),r=s+Vt.length}let o=e.slice(r),i=o.indexOf(kt);if(i!==-1)return n+o.slice(0,i);let a=o.indexOf("{");return a!==-1?n+o.slice(0,a):n+o.slice(0,o.length-xa(o))}function xa(e){let t=Math.min(e.length,kt.length-1);for(let n=t;n>0;n--)if(e.endsWith(kt.slice(0,n)))return n;return 0}function fo(e,t,n){let r=e.indexOf(kt,t);if(r!==-1&&r<n)return r;let o=e.indexOf("{",t);return o===-1||o>n?t:o}function go(e){let t=[],n=0;for(;;){let r=e.indexOf(Vt,n);if(r===-1)return t;let o=fo(e,n,r);e.startsWith(kt,o)&&(o+=kt.length),t.push(e.slice(o,r).trim()),n=r+Vt.length}}ln();function Wo(){if(typeof navigator>"u")return!1;let e=navigator.userAgent??"";if(/Android|iPhone|iPad|iPod|Mobile|Silk|Kindle/i.test(e))return!0;let t=navigator.maxTouchPoints??0;return/Macintosh/i.test(e)&&t>1}var Pu=5,Pr=null,Ou="bonsai-kernels";function Gi(e,t){let n=null,r="",o=!1,i=null,a=u=>e.postMessage(u);function s(u){if(i)return;i=u,o=!0,n=null;let c="";/watchdog|timeout|tdr|hung|exceeded.*deadline/i.test(u)?c=" This is a GPU watchdog timeout (TDR), usually caused by an integrated GPU or weak adapter taking too long on a compute batch. The safest path is a smaller model or the hosted inference ladder. Retrying WILL reset the driver again.":/destroyed/i.test(u)?c=" (This is an expected shutdown, not an error.)":c=" The GPU device was torn away by the OS (possibly due to an overheating shutdown, driver crash, or resource exhaustion). Retrying may succeed after a delay, but is risky.",a({type:"error",fatal:"device-lost",message:`bonsai: GPU device lost (${u}).${c} Not retrying automatically \u2014 retrying without addressing the root cause will re-trigger the same reset.`})}async function l(u){if(i)return a({type:"error",fatal:"device-lost",message:`bonsai: refusing to load \u2014 the GPU device was already lost (${i}).`});try{let c=await t.acquireDevice();c.lost&&c.lost.then(m=>{m?.reason!=="destroyed"&&s(`${m?.reason??"unknown"}: ${m?.message??"no detail"}`)}),c.addEventListener?.("uncapturederror",m=>{let g=m?.error?.message??"unknown GPU error";console.error(`[bonsai] uncaptured GPU error: ${g}`),/out of memory|allocation/i.test(g)?(a({type:"error",message:`bonsai: the GPU ran out of memory (${g}). This model is too large for this adapter \u2014 choose a smaller size.`}),o=!0):/timeout|watchdog|tdr/i.test(g)?s(`watchdog: ${g}`):console.warn(`[bonsai] uncaptured GPU error ignored: ${g} \u2014 generation may continue but results may be corrupt`)});let p=await t.loadKernels();n=mo({device:c,kernelSources:p}),r=u,await n.load({modelUrl:t.resolveModelUrl(u),...t.resolveMirrorUrls?{mirrorUrls:t.resolveMirrorUrls(u)}:{},onProgress:m=>a({type:"progress",progress:m.percent,file:m.detail})}),a({type:"ready",modelId:u});let h=u;n.warm({cancelled:()=>r!==h}).then(m=>{r===h&&console.log(`[bonsai] warm: ${m} block(s) resident before first turn`)}).catch(()=>{})}catch(c){let p=c.message,h=p.includes("token_embd")||p.includes("output_norm")?" This usually means the model download was interrupted or the browser cache is corrupted. Try: (1) clear site data and reload, (2) try a smaller model (Bonsai 4B is 545 MB), or (3) run locally with `pip install awdk && adk bonsai-local` for a faster, more reliable experience.":"";a({type:"error",message:`bonsai load failed: ${p}${h}`})}}async function d(u){if(i)return a({type:"error",fatal:"device-lost",message:`bonsai: cannot generate \u2014 the GPU device was lost (${i}).`});if(!n?.loaded)return a({type:"error",message:"no model loaded \u2014 send {type:'load'} first"});o=!1;try{let{tokenizer:c,config:p,device:h,pipelines:m,weights:g}=n.loaded,f=u.maxTokens??256,b=u.temperature??.7,_=u.topK??20,y=u.topP??.95,v=u.repetitionPenalty??1.1,A=u.reasoningBudget??Math.max(32,f-128),E=c.encodeChat(u.messages,u.tools),O=E.length,k=8192,$=globalThis.__BONSAI_PREFIX_DISABLE!==!0,{kvBytesPerPosition:V,kvBudgetBytes:K,planKvCapacity:Lt}=await Promise.resolve().then(()=>(Qo(),jo)),{resolveKvMode:At,supports4bitKv:pt}=await Promise.resolve().then(()=>(rr(),nr)),Be=p.keyLength??p.embeddingLength/p.headCount,H=pt(Be),ie=At(),S=ie==="4bit"&&!H?"f32":ie;S!==ie&&console.log(`[bonsai] kv: 4-bit unsupported at head_dim=${Be} (kernel row width is 128) \u2014 falling back to the f32 cache for this model`);let nt=S==="4bit"?.5:4,De=Lt({promptLen:O,maxTokens:f,ceiling:k,bytesPerPosition:V({fullAttnLayerCount:p.fullAttnLayers.length,headCountKv:p.headCountKv,headDim:Be},nt),budgetBytes:K(globalThis.navigator?.deviceMemory),reuseEnabled:$}),Qe=De.capacity;if(console.log(`[bonsai] kv capacity ${Qe} \u2014 ${De.reason}`),O+f+1>k)return a({type:"error",message:`context too long: prompt ${O} + maxTokens ${f} > ${k} KV slots. Shorten the prompt or lower maxTokens (in-browser Bonsai is capped at ${k} tokens).`});let{F32KvCache:rt}=await Promise.resolve().then(()=>(Ho(),zo)),{KvCache:mt}=await Promise.resolve().then(()=>(rr(),nr)),{SsmState:ht}=await Promise.resolve().then(()=>(Xo(),Yo)),{f32Buffer:me,sampleToken:ft,sampleTiming:qe}=await Promise.resolve().then(()=>(We(),xt));qe.readbackMs=0,qe.selectMs=0,qe.calls=0;let{prefill:he,decodeStep:ot}=await Promise.resolve().then(()=>(ln(),Jn)),{embedTokens:it}=await Promise.resolve().then(()=>(Vn(),Fo)),{planReuse:ze,committedTokens:Pe,cacheSignature:Me}=await Promise.resolve().then(()=>(Zo(),Jo)),ge=Pr;Pr=null;let Oe=p.keyLength??p.embeddingLength/p.headCount,He=Me({modelId:r||"unknown",quantType:String(g.weightQuantType()),blockCount:p.blockCount,embeddingLength:p.embeddingLength,headCountKv:p.headCountKv,headDim:Oe,linearAttnLayerCount:p.linearAttnLayers.length,kvMode:S}),Ue=p.linearAttnLayers.length===0,J=ze({cache:ge&&ge.device===h?ge:null,promptIds:E,signature:He,maxNewTokens:f,canTruncate:Ue,disabled:globalThis.__BONSAI_PREFIX_DISABLE===!0}),be=J.mode==="extend"&&ge!==null,U=be?ge.kv:S==="4bit"?new mt(h,{fullAttnLayers:p.fullAttnLayers,headCountKv:p.headCountKv,headDim:Oe,capacity:Qe},m):new rt(h,{fullAttnLayers:p.fullAttnLayers,headCountKv:p.headCountKv,headDim:Oe,capacity:Qe}),M=p.deltaNet,te=be?ge.ssm:new ht(h,{linearAttnLayers:p.linearAttnLayers,heads:M?.numVHeads??0,dK:M?.headDim??0,dV:M?.headDim??0,dConv:M?.convKernel,ssmInnerSize:M?.vDim,convDim:M?.convDim});if(!M&&p.linearAttnLayers.length>0)throw new Error(`bonsai-worker: ${p.linearAttnLayers.length} DeltaNet layers were classified but the model exposes no ssm.* geometry \u2014 refusing to run them with zero dims.`);if(be?U.truncate(J.reuseLen):(U.reset(),te.reset()),U.filledLength()!==J.reuseLen)throw new Error(`bonsai-prefix: KV length ${U.filledLength()} != planned reuse ${J.reuseLen} \u2014 refusing to prefill at a position the cache does not end on`);let Z=J.prefillIds;J.savedTokens>0?console.log(`[bonsai] prefix reuse: ${J.savedTokens}/${O} tokens reused (${J.reason}); prefilling ${Z.length}`):console.log(`[bonsai] prefix reuse: none \u2014 ${J.reason}`);let le=me(h,Z.length*p.embeddingLength,"hidden_prefill"),Ie=g.weightQuantType(),N={device:h,pipelines:m,weights:g,config:p,kv:U,kvMode:S,ssm:te,quantType:Ie};a({type:"progress",progress:10,file:`prefill ${O} tokens (running ${p.blockCount} layers)`});let Fe=Date.now();console.log(`[bonsai] prefill start: ${O} tokens \xD7 ${p.blockCount} layers`);let ne=await he(N,le,Z,c,(T,w)=>{a({type:"progress",progress:10+Math.floor(T/w*30),file:`warming layer ${T+1}/${w}`})},J.reuseLen);if(U.filledLength()!==O)throw new Error(`bonsai-prefix: after prefill KV length ${U.filledLength()} != prompt ${O}`);let Te=Date.now()-Fe;if(console.log(`[bonsai] prefill done in ${Te}ms (${(Te/O).toFixed(0)}ms/token)`),tt()){let{readbackF32:T}=await Promise.resolve().then(()=>(We(),xt)),w=await T({device:h,pipelines:m},ne.logits,c.vocabSize),P=1/0,B=-1/0,W=0,ae=0;for(let R=0;R<w.length;R++){let I=w[R];if(!Number.isFinite(I)){ae++;continue}I<P&&(P=I),I>B&&(B=I),W+=I}let ee=W/w.length,F=0;for(let R=0;R<w.length;R++){let I=w[R];Number.isFinite(I)&&(F+=(I-ee)*(I-ee))}let C=Math.sqrt(F/w.length),se=32,j=[],fe=-1/0;for(let R=0;R<w.length;R++){let I=w[R];if(j.length===se&&I<=fe)continue;let re=j.length;for(;re>0&&w[j[re-1]]<I;)re--;j.splice(re,0,R),j.length>se&&j.pop(),fe=w[j[j.length-1]]}let Ne=j.map(R=>{let I=c.decode([R]).replace(/\n/g,"\\n").slice(0,14);return`${R}:${w[R].toFixed(3)}"${I}"`});console.log(`[bonsai] LOGITS vocab=${w.length} bad=${ae} min=${P.toFixed(3)} max=${B.toFixed(3)} mean=${ee.toFixed(4)} sd=${C.toFixed(4)} margin(top1-top2)=${(w[j[0]]-w[j[1]]).toFixed(4)}`),console.log(`[bonsai] LOGITS_TOP32 ${Ne.join(" ")}`),console.log(`[bonsai] LOGITS_STOPS ${[...c.stopIds].map(R=>`${R}=${w[R]?.toFixed(3)}`).join(" ")}`)}if(globalThis.__BONSAI_PREFILL_DIFF===!0&&O>=3){let T=globalThis,w=O-2;T.__BONSAI_ROWS={},T.__BONSAI_CAPTURE_POS=w,U.reset(),te.reset(),T.__BONSAI_CAPTURE_TAG="PN";let P=me(h,O*p.embeddingLength,"hidden_pN");await he(N,P,E,c),U.reset(),te.reset(),T.__BONSAI_CAPTURE_TAG="PM";let B=globalThis.__BONSAI_DETERMINISM===!0,W=B?E:E.slice(0,-1);B&&console.log(`[bonsai] DETERMINISM CONTROL: both runs use the SAME ${O} tokens; expect 0 differing everywhere`);let ae=me(h,W.length*p.embeddingLength,"hidden_pM");if(await he(N,ae,W,c),B){U.reset(),te.reset(),T.__BONSAI_CAPTURE_TAG="PC";let F=me(h,O*p.embeddingLength,"hidden_pC");await he(N,F,E,c)}T.__BONSAI_CAPTURE_TAG=void 0,T.__BONSAI_CAPTURE_POS=void 0;let ee=T.__BONSAI_ROWS??{};if(B)for(let F=0;F<p.blockCount;F++){let C=ee[`PM:${F}`],se=ee[`PC:${F}`];if(!C||!se||C.length!==se.length)continue;let j=0,fe=0,Ne=0,R=0;for(let I=0;I<C.length;I++){let re=Math.abs(C[I]-se[I]);re>j&&(j=re),fe+=re,Ne+=Math.abs(C[I]),re>1e-6&&R++}console.log(`[bonsai] WARMDIFF L${F} kind=${p.layerKinds[F]} maxAbs=${j.toExponential(3)} relative=${(fe/(Ne||1)).toExponential(3)} differing=${R}/${C.length}`)}for(let F=0;F<p.blockCount;F++){let C=ee[`PN:${F}`],se=ee[`PM:${F}`];if(!C||!se||C.length!==se.length)continue;let j=0,fe=0,Ne=0,R=0;for(let I=0;I<C.length;I++){let re=Math.abs(C[I]-se[I]);re>j&&(j=re),fe+=re,Ne+=Math.abs(C[I]),re>1e-6&&R++}console.log(`[bonsai] PREFILLDIFF L${F} kind=${p.layerKinds[F]} pos=${w} maxAbs=${j.toExponential(3)} relative=${(fe/(Ne||1)).toExponential(3)} differing=${R}/${C.length}`)}T.__BONSAI_ROWS={},U.reset(),te.reset(),await he(N,le,E,c)}if(globalThis.__BONSAI_DECODE_DIFF===!0&&O>=2){let{readbackF32:T}=await Promise.resolve().then(()=>(We(),xt)),w=globalThis,P=c.vocabSize;w.__BONSAI_ROWS={},U.reset(),te.reset(),w.__BONSAI_CAPTURE_TAG="A";let B=me(h,O*p.embeddingLength,"hidden_diffA"),W=await he(N,B,E,c),ae=Array.from(await T({device:h,pipelines:m},W.logits,P));U.reset(),te.reset(),w.__BONSAI_CAPTURE_TAG=void 0;let ee=E.slice(0,-1),F=me(h,ee.length*p.embeddingLength,"hidden_diffB");await he(N,F,ee,c);let C=globalThis.__BONSAI_INJECT_LAYER;if(typeof C=="number"&&Number.isFinite(C)){let Y=(w.__BONSAI_ROWS??{})[`A:${C}`];Y&&(globalThis.__BONSAI_INJECT={layer:C,row:Y},console.log(`[bonsai] INJECT armed at L${C}`))}w.__BONSAI_CAPTURE_TAG="B";let se=me(h,p.embeddingLength,"hidden_diffDec");await it(N,[E[O-1]],se,g,p.embeddingLength);let j=await ot(N,se,O-1,c),fe=Array.from(await T({device:h,pipelines:m},j.logits,P));w.__BONSAI_CAPTURE_TAG=void 0;let Ne=w.__BONSAI_ROWS??{};for(let Y=0;Y<p.blockCount;Y++){let ce=Ne[`A:${Y}`],bt=Ne[`B:${Y}`];if(!ce||!bt||ce.length!==bt.length)continue;let Re=0,An=0,Fr=0,Kr=0;for(let Et=0;Et<ce.length;Et++){let Ft=Math.abs(ce[Et]-bt[Et]);Ft>Re&&(Re=Ft),An+=Ft,Fr+=Math.abs(ce[Et]),Ft>1e-6&&Kr++}console.log(`[bonsai] BLOCKDIFF L${Y} kind=${p.layerKinds[Y]} maxAbs=${Re.toExponential(3)} meanAbs=${(An/ce.length).toExponential(3)} relative=${(An/(Fr||1)).toExponential(3)} differing=${Kr}/${ce.length}`)}let R=0,I=0,re=0;for(let Y=0;Y<P;Y++){if(!Number.isFinite(fe[Y])){re++;continue}let ce=Math.abs(ae[Y]-fe[Y]);ce>R&&(R=ce),I+=ce}let Ut=Y=>{let ce=-1,bt=-1/0;for(let Re=0;Re<P;Re++)Number.isFinite(Y[Re])&&Y[Re]>bt&&(bt=Y[Re],ce=Re);return ce};console.log(`[bonsai] DECODE_DIFF pos=${O-1} maxAbs=${R.toFixed(4)} meanAbs=${(I/P).toFixed(6)} nonFiniteB=${re} argmaxA=${Ut(ae)} argmaxB=${Ut(fe)} argmaxAgree=${Ut(ae)===Ut(fe)}`),globalThis.__BONSAI_INJECT=void 0,w.__BONSAI_ROWS={},U.reset(),te.reset(),await he(N,le,E,c)}let Ee=me(h,p.embeddingLength,"hidden_decode"),gt={device:h,pipelines:m,quantType:Ie},$e=globalThis.__BONSAI_TIMING===!0,Tt=Wo(),we={embed:0,forward:0,sample:0,tokens:0},Ir=[],$r=async(T,w)=>{Ir.push(T);let P=$e?performance.now():0;await it(N,[T],Ee,g,p.embeddingLength);let B=$e?performance.now():0,W=(await ot(N,Ee,w,c)).logits;return($e||Tt)&&await h.queue.onSubmittedWorkDone(),$e&&(we.embed+=B-P,we.forward+=performance.now()-B,we.tokens++),W},Fi="\uFFFD",Nr=T=>{let w=c.decode(T),P=w.length;for(;P>0&&w[P-1]===Fi;)P--;return w.slice(0,P)},vn=[],xn=[],Dt="",Rr="",Ki=(T,w,P)=>{let B=Nr(T);return B.length>w.length&&B.startsWith(w)?(a({type:"token",text:B.slice(w.length),channel:P}),B):B.length>=w.length?B:w},Gr=!!(u.tools&&u.tools.length>0),qt="",Cr=0,Wi=T=>{let w=Nr(T),P=ho(w,Gr);if(P.length>qt.length&&P.startsWith(qt)&&(a({type:"token",text:P.slice(qt.length),channel:"answer"}),qt=P),Gr){let B=go(w);for(let W=Cr;W<B.length;W++)a({type:"token",text:B[W],channel:"tool"});Cr=B.length}return w},at=!1;if(c.thinkEndId!==void 0&&c.thinkStartId!==void 0){let T=E.lastIndexOf(c.thinkStartId),w=E.lastIndexOf(c.thinkEndId);at=T!==-1&&T>w}let Dr=!1,ji=64,Mt=[],Sn=ne.logits,qr=O,ye=0,Ln="max-tokens",Mr=Date.now();for(;ye<f&&!o;){let T=$e?performance.now():0,w=await ft(gt,Sn,c.vocabSize,{temperature:b,topK:_,topP:y,repetitionPenalty:v,recentIds:Mt});if($e&&(we.sample+=performance.now()-T),ye++,c.isStop(w)){Ln="stop-token";break}Mt.push(w),Mt.length>ji&&Mt.shift(),w===c.thinkEndId?at=!1:w===c.thinkStartId?at=!0:at?(vn.push(w),Dt=Ki(vn,Dt,"thinking")):(xn.push(w),Rr=Wi(xn)),Sn=await $r(w,qr++),at&&!Dr&&c.thinkEndId!==void 0&&ye>=A&&(Dr=!0,at=!1,Sn=await $r(c.thinkEndId,qr++),a({type:"progress",file:"reasoning budget reached \u2014 answering"})),Tt&&await new Promise(ae=>setTimeout(ae,0));let P=10+Math.floor(ye/f*80),B=ye/((Date.now()-Mr)/1e3),W=at?"thinking":"answering";a({type:"progress",progress:P,file:`${W} \xB7 ${ye} tok \xB7 ${B.toFixed(1)} tok/s`}),(ye===1||ye%10===0)&&console.log(`[bonsai] ${ye} tokens \xB7 ${B.toFixed(2)} tok/s (${W})`)}o&&(Ln="interrupted");let Qi=Date.now()-Mr,zi=ye>0?ye/Qi*1e3:0,Ur=Rr.trim()||(Dt.trim()?"I ran out of room to finish that thought \u2014 my reasoning is above. Ask again and I'll be more direct.":"");if(console.log(`[bonsai] done: ${ye} tok, ${Ln}, think=${vn.length} answer=${xn.length}`),$e&&we.tokens>0){let T=we.tokens,w=ae=>(ae/T).toFixed(1),P=we.embed+we.forward+we.sample,B=qe,W=B.calls>0?` [readback=${w(B.readbackMs)}ms select=${w(B.selectMs)}ms]`:"";console.log(`[bonsai] TIMING per token over ${T}: embed=${w(we.embed)}ms forward=${w(we.forward)}ms sample=${w(we.sample)}ms${W} (${w(P)}ms total, ${(1e3/(P/T)).toFixed(1)} tok/s implied)`)}if(u.tools&&u.tools.length>0){let{setToolContext:T,drainToolActions:w}=await Promise.resolve().then(()=>(Br(),Bi));T(u.context??{});let{orchestrateToolCalls:P,extendMessagesWithToolResults:B}=await Promise.resolve().then(()=>(Ri(),Ni)),{finalText:W,toolCalls:ae}=await P(Ur,ee=>{ee.type==="tool_result"&&a({type:"progress",file:`executed ${ee.toolName}: ${ee.result}`})},Pu);if(ae.length>0){let ee=w();ee.length>0&&a({type:"tool_action",actions:ee});let{drainImages:F}=await Promise.resolve().then(()=>(ar(),ei)),C=F();C.length>0&&a({type:"image",images:C});let se=W.trim()?[...u.messages,{role:"assistant",content:W}]:[...u.messages];await d({...u,messages:B(se,ae),tools:void 0});return}}Pr={device:h,kv:U,ssm:te,tokens:Pe(E,Ir),signature:He,capacity:U.capacity},a({type:"done",text:Ur,reasoning:Dt.trim()||void 0,tokensPerSecond:zi})}catch(c){let p=c.message,m=p.includes("not loaded")&&(p.includes("token_embd")||p.includes("output_norm"))?" The model weights were not fully downloaded. Clear your browser's site data (Settings \u2192 Privacy \u2192 Clear browsing data \u2192 Cached images and files), then reload this page to re-download the model. Or run locally: `pip install awdk && adk bonsai-local` for GPU-accelerated inference.":"";a({type:"error",message:`bonsai generate failed: ${p}${m}`})}}e.addEventListener("message",u=>{let c=u.data;c.type==="load"?l(c.modelId):c.type==="generate"?d(c):c.type==="interrupt"&&(o=!0)})}var Iu=`// SPDX-License-Identifier: LicenseRef-Aitherium-Proprietary
// \xA9 2026 Aitherium, LLC. Original work.
// Original Aitherium WebGPU implementation \u2014 WGSL kernels ported from the PrismML
// llama.cpp fork (github.com/PrismML-Eng/llama.cpp @ prism, Aitherium/PrismML-owned).
// NO third-party Space code (HF Spaces bonsai-* explicitly excluded).
// Numerics ported from owner-owned fork: github.com/PrismML-Eng/llama.cpp @ branch "prism"
//   - depthwise causal 1-D conv over q/k/v projections (short left-padded kernel).
//
// Part of the DeltaNet linear-attention path. Depthwise (per-channel) causal convolution
// with left padding = kernel_size-1, followed by the activation applied in the caller.
// Matches the fork ssm_conv1d contract; the exact activation ordering is transcribed in
// deltanet.wgsl. State carry for streaming decode lives in ssm_state.ts.

struct ConvP {
  n_tokens : u32,
  channels : u32,
  kernel   : u32,   // ssm.conv_kernel
  _p0 : u32,
};

@group(0) @binding(0) var<storage, read>       x       : array<f32>;   // [n_tokens * channels]
@group(0) @binding(1) var<storage, read>       weight  : array<f32>;   // [channels * kernel]
@group(0) @binding(2) var<storage, read>       bias    : array<f32>;   // [channels]
@group(0) @binding(3) var<storage, read_write> out     : array<f32>;   // [n_tokens * channels]
@group(0) @binding(4) var<uniform>             p       : ConvP;

@compute @workgroup_size(64)
fn main(@builtin(workgroup_id) wg_ : vec3<u32>,
        @builtin(local_invocation_id) lid_ : vec3<u32>,
        @builtin(num_workgroups) nwg_ : vec3<u32>) {
  // one thread per (token, channel)
  // FLAT INDEX ACROSS A POSSIBLY-2D WORKGROUP GRID.
  // dispatch1D() folds the workgroup count into y once it passes WebGPU's 65535-per-dimension
  // limit. When it does not \u2014 the common case \u2014 num_workgroups.y is 1 and this reduces to
  // EXACTLY the old expression, so the working 27B numerics are untouched.
  let idx = (wg_.x + wg_.y * nwg_.x) * 64u + lid_.x;
  let total = p.n_tokens * p.channels;
  if (idx >= total) { return; }
  let token = idx / p.channels;
  let ch    = idx % p.channels;

  var sum : f32 = bias[ch];
  for (var kk : u32 = 0u; kk < p.kernel; kk = kk + 1u) {
    // causal: output token t depends on inputs [t-(kernel-1) .. t]; left-pad with 0
    let offset = i32(token) - i32(p.kernel - 1u - kk);
    if (offset >= 0) {
      let xv = x[u32(offset) * p.channels + ch];
      sum = sum + xv * weight[ch * p.kernel + kk];
    }
  }
  out[idx] = sum;   // activation applied by caller (SiLU) per fork ordering
}
`,$u=`// ============================================================================
// DEPRECATED / NOT ON THE LIVE PATH (verified 2026-07-24).
//
// Token generation uses deltanet_seq.wgsl. This kernel's only dispatcher is
// ops.ts::deltanetStep, which has ZERO callers.
//
// IT ALSO CARRIES OLDER RECURRENCE ALGEBRA than deltanet_seq.wgsl (decay applied
// at a different point), so reading it as "the" delta rule will mislead you \u2014
// an ultracode pass did exactly that. Diff against deltanet_seq.wgsl, or better
// against the authoritative fork:
//   github.com/PrismML-Eng/llama.cpp @ prism
//     src/models/delta-net-base.cpp :: build_delta_net_autoregressive
//     src/models/qwen35.cpp         :: build_layer_attn_linear
// (that repo is PUBLIC and fetchable.)
//
// Do not "fix" this file expecting model behaviour to change.
// ============================================================================
// SPDX-License-Identifier: LicenseRef-Aitherium-Proprietary
// \xA9 2026 Aitherium, LLC. Original work.
// Original Aitherium WebGPU implementation \u2014 WGSL kernels ported from the PrismML
// llama.cpp fork (github.com/PrismML-Eng/llama.cpp @ prism, Aitherium/PrismML-owned).
// NO third-party Space code (HF Spaces bonsai-* explicitly excluded).
// Numerics ported from owner-owned fork: github.com/PrismML-Eng/llama.cpp @ branch "prism"
//   - gated DeltaNet recurrence ...... src/llama-model.cpp:1797-1799 (qwen35 shares QWEN3NEXT SSM path)
//   - ssm tensors .................... src/llama-arch.cpp:431-439 (ssm_conv1d/beta/g_a/g_b/a/norm)
//
// HIGHEST ARCHITECTURAL RISK (\xA78 risk #2). Gated delta-rule linear attention on the 48
// linear layers. Per-head state matrix S (d_k x d_v) persisted across decode steps
// (ssm_state.ts). This is the SINGLE-STEP DECODE recurrence (one token). Prefill uses a
// chunked/parallel scan built on the same algebra (implemented in TS-driven dispatch).
//
// Recurrence (per token), transcribed from the fork's DeltaNet reference:
//     err = v - S^T k            (d_v)          "delta" prediction error
//     S   = S * diag(g)          (decay/gate along d_k)
//     S   = S + beta * (k outer err)            rank-1 update
//     o   = S^T q                (d_v)          output
//
// CORRECTNESS NOTE: the exact placement of the gate (before vs after the delta term) and
// whether beta multiplies err or (err scaled) MUST be pinned by the Milestone-5 golden
// vector against the fork before this layer is trusted. The structure below encodes the
// design's stated form; it is the reference the M5 test validates, not an assumed-correct
// final answer.  One workgroup per head; S held in storage (persisted between calls).

struct DeltaP {
  d_k   : u32,
  d_v   : u32,
  head  : u32,
  _p0   : u32,
};

@group(0) @binding(0) var<storage, read>        q     : array<f32>;   // [d_k]
@group(0) @binding(1) var<storage, read>        k     : array<f32>;   // [d_k]
@group(0) @binding(2) var<storage, read>        v     : array<f32>;   // [d_v]
@group(0) @binding(3) var<storage, read>        g     : array<f32>;   // [d_k] gate (diag)
@group(0) @binding(4) var<storage, read>        beta  : array<f32>;   // [1] scalar beta
@group(0) @binding(5) var<storage, read_write>  state : array<f32>;   // [d_k * d_v] persisted S
@group(0) @binding(6) var<storage, read_write>  out   : array<f32>;   // [d_v]
@group(0) @binding(7) var<uniform>              p     : DeltaP;

@compute @workgroup_size(1)
fn main() {
  let dk = p.d_k;
  let dv = p.d_v;
  let b  = beta[0];

  // err = v - S^T k    (S is [d_k x d_v], row i = state[i*dv + j])
  var err : array<f32, 256>;   // d_v <= 256
  for (var j : u32 = 0u; j < dv; j = j + 1u) {
    var sTk : f32 = 0.0;
    for (var i : u32 = 0u; i < dk; i = i + 1u) {
      sTk = sTk + state[i * dv + j] * k[i];
    }
    err[j] = v[j] - sTk;
  }

  // S = S*diag(g) + beta * (k outer err); then o = S^T q
  var o : array<f32, 256>;
  for (var j : u32 = 0u; j < dv; j = j + 1u) { o[j] = 0.0; }

  for (var i : u32 = 0u; i < dk; i = i + 1u) {
    let gi = g[i];
    let ki = k[i];
    let qi = q[i];
    for (var j : u32 = 0u; j < dv; j = j + 1u) {
      let s_new = state[i * dv + j] * gi + b * ki * err[j];
      state[i * dv + j] = s_new;
      o[j] = o[j] + s_new * qi;   // accumulate S^T q with the updated state
    }
  }

  for (var j : u32 = 0u; j < dv; j = j + 1u) { out[j] = o[j]; }
}
`,Nu=`// SPDX-License-Identifier: LicenseRef-Aitherium-Proprietary
// \xA9 2026 Aitherium, LLC. Original work.
// Original Aitherium WebGPU implementation \u2014 WGSL kernels ported from the PrismML
// llama.cpp fork (github.com/PrismML-Eng/llama.cpp @ prism, Aitherium/PrismML-owned).
// NO third-party Space code (HF Spaces bonsai-* explicitly excluded).
//
// Gated-DeltaNet per-(token,v-head) scalars, computed from the alpha/beta projections
// and the learnable A_log / dt_bias, exactly as Qwen3-Next's GatedDeltaNet:
//   beta_t = sigmoid(beta_raw)                                  (write strength, (0,1))
//   g_t    = exp( ssm_a * softplus(alpha_raw + dt_bias) ) (decay, (0,1]; ssm_a = -exp(A_log) pre-baked)
// One thread per (token, v-head). H = num_v_heads. Inputs are [n_tokens*H]; A_log and
// dt_bias are per-v-head [H]. softplus is evaluated in the numerically-stable form.

struct GateP { n_tokens : u32, heads : u32, _p0 : u32, _p1 : u32 };

@group(0) @binding(0) var<storage, read>        alpha_raw : array<f32>;   // [n_tokens*H]
@group(0) @binding(1) var<storage, read>        beta_raw  : array<f32>;   // [n_tokens*H]
@group(0) @binding(2) var<storage, read>        a_log     : array<f32>;   // [H]
@group(0) @binding(3) var<storage, read>        dt_bias   : array<f32>;   // [H]
@group(0) @binding(4) var<storage, read_write>  g_out     : array<f32>;   // [n_tokens*H]
@group(0) @binding(5) var<storage, read_write>  beta_out  : array<f32>;   // [n_tokens*H]
@group(0) @binding(6) var<uniform>              p         : GateP;

fn softplus(x : f32) -> f32 {
  // log(1+exp(x)) stable: max(x,0) + log(1 + exp(-|x|))
  return max(x, 0.0) + log(1.0 + exp(-abs(x)));
}

@compute @workgroup_size(64)
fn main(@builtin(workgroup_id) wg_ : vec3<u32>,
        @builtin(local_invocation_id) lid_ : vec3<u32>,
        @builtin(num_workgroups) nwg_ : vec3<u32>) {
  // FLAT INDEX ACROSS A POSSIBLY-2D WORKGROUP GRID.
  // dispatch1D() folds the workgroup count into y once it passes WebGPU's 65535-per-dimension
  // limit. When it does not \u2014 the common case \u2014 num_workgroups.y is 1 and this reduces to
  // EXACTLY the old expression, so the working 27B numerics are untouched.
  let idx = (wg_.x + wg_.y * nwg_.x) * 64u + lid_.x;
  let total = p.n_tokens * p.heads;
  if (idx >= total) { return; }
  let h = idx % p.heads;

  let sp = softplus(alpha_raw[idx] + dt_bias[h]);
  // ssm_a is stored PRE-BAKED as -exp(A_log) in the GGUF (verified: blk.0.ssm_a
  // = -0.2629, negative) - the fork multiplies it in DIRECTLY (qwen35.cpp:
  // "gate = alpha_softplus * ssm_a  // -A_log.exp() * softplus"). Applying
  // -exp() AGAIN gave ~3x wrong decay in all 48 DeltaNet layers.
  let a  = a_log[h] * sp;            // <= 0 (a_log holds -exp(A_log) pre-baked)
  g_out[idx]    = exp(a);            // (0,1]
  beta_out[idx] = 1.0 / (1.0 + exp(-beta_raw[idx]));
}
`,Ru=`// SPDX-License-Identifier: LicenseRef-Aitherium-Proprietary
// \xA9 2026 Aitherium, LLC. Original work.
// Original Aitherium WebGPU implementation \u2014 WGSL kernels ported from the PrismML
// llama.cpp fork (github.com/PrismML-Eng/llama.cpp @ prism, Aitherium/PrismML-owned).
// NO third-party Space code (HF Spaces bonsai-* explicitly excluded).
//
// Gated DeltaNet (Qwen3-Next) sequential recurrence \u2014 the WHOLE token sequence for a
// layer in ONE dispatch, no host readback. Per v-head state S is [d_k \xD7 d_v] (d_k==d_v==
// head_dim). q/k are grouped: each v-head h reads the k/q of k-head (h / v_per_k). q,k are
// already L2-normalized; v is already conv+SiLU'd; g (decay) and beta (write strength) are
// precomputed per (token,v-head) by deltanet_gate.
//
// Recurrence, per token t, per v-head h (from modeling_qwen3_next GatedDeltaNet):
//   Sdec[i,j] = g_t * S[i,j]                    (scalar decay per head/step)
//   kv[j]     = sum_i Sdec[i,j] * k[i]          (retrieve current key)
//   err[j]    = v[j] - kv[j]
//   S[i,j]    = Sdec[i,j] + k[i] * (beta_t * err[j])   (rank-1 write)
//   o[j]      = (sum_i S[i,j] * q[i]) / sqrt(d_k)      (read-out)
//
// Parallelism: one thread per (v-head h, value-column j). Thread (h,j) owns column j of
// head h's state \u2014 columns are disjoint across threads, so the update is race-free and the
// per-token loop runs inside the thread with NO barriers. Grid = heads * head_dim threads.

struct SeqP {
  n_tokens  : u32,
  v_heads   : u32,   // num_v_heads (48)
  k_heads   : u32,   // num_k_heads (16)
  head_dim  : u32,   // d_k == d_v (128)
  v_per_k   : u32,   // v_heads / k_heads (3)
  _p0 : u32, _p1 : u32, _p2 : u32,
};

@group(0) @binding(0) var<storage, read>        q     : array<f32>;   // [n_tokens * k_heads * head_dim]
@group(0) @binding(1) var<storage, read>        k     : array<f32>;   // [n_tokens * k_heads * head_dim]
@group(0) @binding(2) var<storage, read>        v     : array<f32>;   // [n_tokens * v_heads * head_dim]
@group(0) @binding(3) var<storage, read>        gdec  : array<f32>;   // [n_tokens * v_heads]
@group(0) @binding(4) var<storage, read>        beta  : array<f32>;   // [n_tokens * v_heads]
@group(0) @binding(5) var<storage, read_write>  state : array<f32>;   // [v_heads * head_dim * head_dim]
@group(0) @binding(6) var<storage, read_write>  out   : array<f32>;   // [n_tokens * v_heads * head_dim]
@group(0) @binding(7) var<uniform>              p     : SeqP;

@compute @workgroup_size(64)
fn main(@builtin(workgroup_id) wg_ : vec3<u32>,
        @builtin(local_invocation_id) lid_ : vec3<u32>,
        @builtin(num_workgroups) nwg_ : vec3<u32>) {
  let d   = p.head_dim;
  // FLAT INDEX ACROSS A POSSIBLY-2D WORKGROUP GRID.
  // dispatch1D() folds the workgroup count into y once it passes WebGPU's 65535-per-dimension
  // limit. When it does not \u2014 the common case \u2014 num_workgroups.y is 1 and this reduces to
  // EXACTLY the old expression, so the working 27B numerics are untouched.
  let idx = (wg_.x + wg_.y * nwg_.x) * 64u + lid_.x;
  let total = p.v_heads * d;
  if (idx >= total) { return; }

  let h = idx / d;            // v-head
  let j = idx % d;            // value column this thread owns
  // Fork-verified GQA mapping: ggml_repeat_4d TILES cyclically (dst head i1*ne01+k1
  // reads src head k1), so v-head h uses k-head (h % k_heads) - NOT h / v_per_k
  // (interleave). The old mapping paired 32 of 48 v-heads with the wrong q/k.
  let kh = h % p.k_heads;     // shared k/q head for this v-head (cyclic, fork parity)
  let sbase = h * d * d;      // base of head h's [d\xD7d] state
  let inv_scale = inverseSqrt(f32(d));

  for (var t : u32 = 0u; t < p.n_tokens; t = t + 1u) {
    let qb = (t * p.k_heads + kh) * d;
    let vb = (t * p.v_heads + h) * d;
    let g  = gdec[t * p.v_heads + h];
    let b  = beta[t * p.v_heads + h];

    // pass 1: kv[j] = sum_i (g*S[i,j]) * k[i]
    var kv : f32 = 0.0;
    for (var i : u32 = 0u; i < d; i = i + 1u) {
      kv = kv + g * state[sbase + i * d + j] * k[qb + i];
    }
    let err = v[vb + j] - kv;

    // pass 2: write S[:,j] and read out o[j] = (sum_i S_new[i,j] * q[i]) / sqrt(d)
    var o : f32 = 0.0;
    for (var i : u32 = 0u; i < d; i = i + 1u) {
      let s_new = g * state[sbase + i * d + j] + k[qb + i] * (b * err);
      state[sbase + i * d + j] = s_new;
      o = o + s_new * q[qb + i];
    }
    out[vb + j] = o * inv_scale;
  }
}
`,Gu=`// SPDX-License-Identifier: LicenseRef-Aitherium-Proprietary
// \xA9 2026 Aitherium, LLC. Original work.
// Original Aitherium WebGPU implementation \u2014 WGSL kernels ported from the PrismML
// llama.cpp fork (github.com/PrismML-Eng/llama.cpp @ prism, Aitherium/PrismML-owned).
// NO third-party Space code (HF Spaces bonsai-* explicitly excluded).
// Numerics ported from owner-owned fork: github.com/PrismML-Eng/llama.cpp @ branch "prism"
//   - residual add / mul / copy helpers used between decoder sub-layers.
//
// Op selector via a uniform: 0=add, 1=mul, 2=copy(a). Element-wise over length n.

struct EW { n : u32, op : u32, _p0 : u32, _p1 : u32 };

@group(0) @binding(0) var<storage, read>       a   : array<f32>;
@group(0) @binding(1) var<storage, read>       b   : array<f32>;
@group(0) @binding(2) var<storage, read_write> out : array<f32>;
@group(0) @binding(3) var<uniform>             p   : EW;

@compute @workgroup_size(256)
fn main(@builtin(workgroup_id) wg_ : vec3<u32>,
        @builtin(local_invocation_id) lid_ : vec3<u32>,
        @builtin(num_workgroups) nwg_ : vec3<u32>) {
  // FLAT INDEX ACROSS A POSSIBLY-2D WORKGROUP GRID.
  // dispatch1D() folds the workgroup count into y once it passes WebGPU's 65535-per-dimension
  // limit. When it does not \u2014 the common case \u2014 num_workgroups.y is 1 and this reduces to
  // EXACTLY the old expression, so the working 27B numerics are untouched.
  let i = (wg_.x + wg_.y * nwg_.x) * 256u + lid_.x;
  if (i >= p.n) { return; }
  switch (p.op) {
    case 0u: { out[i] = a[i] + b[i]; }   // residual add
    case 1u: { out[i] = a[i] * b[i]; }
    default: { out[i] = a[i]; }          // copy
  }
}
`,Cu=`// SPDX-License-Identifier: LicenseRef-Aitherium-Proprietary
// \xA9 2026 Aitherium, LLC. Original work.
// In-place elementwise (io = io OP b) \u2014 single read_write binding for the accumulator to
// avoid the read/read_write aliasing WebGPU rejects. Pairs with elementwise.wgsl.
// op: 0=add, 1=mul, 2=copy(no-op), 3=silu (unary: io = io*sigmoid(io), b ignored).
struct EW { n : u32, op : u32, _p0 : u32, _p1 : u32 };
@group(0) @binding(0) var<storage, read_write> io : array<f32>;
@group(0) @binding(1) var<storage, read>       b  : array<f32>;
@group(0) @binding(2) var<uniform>             p  : EW;
@compute @workgroup_size(256)
fn main(@builtin(workgroup_id) wg_ : vec3<u32>,
        @builtin(local_invocation_id) lid_ : vec3<u32>,
        @builtin(num_workgroups) nwg_ : vec3<u32>) {
  // FLAT INDEX ACROSS A POSSIBLY-2D WORKGROUP GRID.
  // dispatch1D() folds the workgroup count into y once it passes WebGPU's 65535-per-dimension
  // limit. When it does not \u2014 the common case \u2014 num_workgroups.y is 1 and this reduces to
  // EXACTLY the old expression, so the working 27B numerics are untouched.
  let i = (wg_.x + wg_.y * nwg_.x) * 256u + lid_.x;
  if (i >= p.n) { return; }
  switch (p.op) {
    case 0u: { io[i] = io[i] + b[i]; }
    case 1u: { io[i] = io[i] * b[i]; }
    case 3u: { let z = io[i]; io[i] = z / (1.0 + exp(-z)); }   // SiLU
    case 4u: { io[i] = io[i] / (1.0 + exp(-b[i])); }          // io *= sigmoid(b) (attn out-gate)
    default: { }
  }
}
`,Du=`// SPDX-License-Identifier: LicenseRef-Aitherium-Proprietary
// \xA9 2026 Aitherium, LLC. Original work.
//
// The four ops the Flux2 MMDiT needs that the LLM kernels do not provide. Every one of
// them has a CPU counterpart in \`image/mmdit.ts\`, which is differentially verified
// against a real reference forward (37 stages, 5e-5), and every one is compared against
// that counterpart on a real GPU by \`e2e/bonsai-image-gpu-differential.mjs\`.
//
// \u{1F6A8} WHY THESE ARE NEW RATHER THAN REUSED. Three of the four look like kernels that
// already ship, and reusing those would be silently wrong:
//
//   layernorm        NOT rmsnorm.wgsl. RMSNorm does not subtract the mean. Flux2's
//                    modulated norms are LayerNorm with elementwise_affine=FALSE --
//                    mean-centred, and with no learnable weight, because the shift and
//                    scale arrive from the modulation instead. Substituting RMSNorm
//                    changes every activation and raises nothing.
//
//   rope_interleaved NOT rope_imrope.wgsl. That kernel pairs (p, p + rot/2) -- NEOX /
//                    half-split -- and its own comment records that as a FIX ("the old
//                    (2p, 2p+1) pairing scrambled positional phase"), which is true for
//                    the LLM and exactly backwards here. Flux2 pairs ADJACENT
//                    components (2p, 2p+1), from diffusers' use_real_unbind_dim=-1.
//                    Asserted in both directions by
//                    \`e2e/bonsai-image-kernel-conventions.mjs\`.
//
//   modulate         x * (1 + scale) + shift, with scale/shift broadcast over tokens.
//                    Not elementwise.wgsl: the operands have different ranks.
//
//   add_gated        x + gate * delta, gate broadcast over tokens. The residual add of
//                    every block; separate from \`modulate\` because fusing them would
//                    force a caller that needs only one to supply dummies for the other.
//
// LAYOUT, shared by all four: activations are [token][channel] row-major, and for the
// RoPE kernel [token][head][dim] -- the reference unflattens the projection to
// (heads, headDim) on the LAST axis, so head h of token t is contiguous. Reading it as
// [head][token][dim] transposes silently and is shape-compatible.

// \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500 LayerNorm \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
// One workgroup per TOKEN, cooperating over that token's channels. Not one thread per
// token: dim is 3072 in this model, and a single lane walking it is the one-lane mistake
// that made attention 8x slower than it had to be.
//
// Two passes (mean, then variance) rather than the sum/sum-of-squares trick: at f32 the
// one-pass form loses precision exactly where the variance is small, and a modulated
// norm's input is centred by construction.

struct LnP {
  dim : u32,
  eps : f32,
  _p0 : u32, _p1 : u32,
};

@group(0) @binding(0) var<storage, read>       ln_x : array<f32>;
@group(0) @binding(1) var<storage, read_write> ln_y : array<f32>;
@group(0) @binding(2) var<uniform>             lnp  : LnP;

var<workgroup> ln_red : array<f32, 256>;

@compute @workgroup_size(256)
fn layernorm_main(@builtin(workgroup_id) wg : vec3<u32>,
                  @builtin(local_invocation_id) lid : vec3<u32>) {
  let t = wg.x;
  let base = t * lnp.dim;
  let tid = lid.x;

  var s : f32 = 0.0;
  var i : u32 = tid;
  loop {
    if (i >= lnp.dim) { break; }
    s = s + ln_x[base + i];
    i = i + 256u;
  }
  ln_red[tid] = s;
  workgroupBarrier();
  var stride : u32 = 128u;
  loop {
    if (stride == 0u) { break; }
    if (tid < stride) { ln_red[tid] = ln_red[tid] + ln_red[tid + stride]; }
    workgroupBarrier();
    stride = stride >> 1u;
  }
  let mean = ln_red[0] / f32(lnp.dim);
  workgroupBarrier();

  var v : f32 = 0.0;
  i = tid;
  loop {
    if (i >= lnp.dim) { break; }
    let d = ln_x[base + i] - mean;
    v = v + d * d;
    i = i + 256u;
  }
  ln_red[tid] = v;
  workgroupBarrier();
  stride = 128u;
  loop {
    if (stride == 0u) { break; }
    if (tid < stride) { ln_red[tid] = ln_red[tid] + ln_red[tid + stride]; }
    workgroupBarrier();
    stride = stride >> 1u;
  }
  let inv = inverseSqrt(ln_red[0] / f32(lnp.dim) + lnp.eps);
  workgroupBarrier();

  // NO learnable affine here on purpose: elementwise_affine=false. The shift and scale
  // come from \`modulate\`, and applying one here would double-apply the conditioning.
  i = tid;
  loop {
    if (i >= lnp.dim) { break; }
    ln_y[base + i] = (ln_x[base + i] - mean) * inv;
    i = i + 256u;
  }
}

// \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500 modulate \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
// y[t, c] = x[t, c] * (1 + scale[c]) + shift[c]

struct ModP {
  dim    : u32,
  tokens : u32,
  _p0 : u32, _p1 : u32,
};

@group(0) @binding(0) var<storage, read>       md_x     : array<f32>;
@group(0) @binding(1) var<storage, read>       md_shift : array<f32>;
@group(0) @binding(2) var<storage, read>       md_scale : array<f32>;
@group(0) @binding(3) var<storage, read_write> md_y     : array<f32>;
@group(0) @binding(4) var<uniform>             mdp      : ModP;

@compute @workgroup_size(64)
fn modulate_main(@builtin(global_invocation_id) gid : vec3<u32>,
                 @builtin(num_workgroups) nwg : vec3<u32>) {
  let total = mdp.tokens * mdp.dim;
  let idx = gid.x + gid.y * nwg.x * 64u;
  if (idx >= total) { return; }
  let c = idx % mdp.dim;
  md_y[idx] = md_x[idx] * (1.0 + md_scale[c]) + md_shift[c];
}

// \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500 add_gated \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
// y[t, c] = x[t, c] + gate[c] * delta[t, c]

@group(0) @binding(0) var<storage, read>       ag_x     : array<f32>;
@group(0) @binding(1) var<storage, read>       ag_delta : array<f32>;
@group(0) @binding(2) var<storage, read>       ag_gate  : array<f32>;
@group(0) @binding(3) var<storage, read_write> ag_y     : array<f32>;
@group(0) @binding(4) var<uniform>             agp      : ModP;

@compute @workgroup_size(64)
fn add_gated_main(@builtin(global_invocation_id) gid : vec3<u32>,
                  @builtin(num_workgroups) nwg : vec3<u32>) {
  let total = agp.tokens * agp.dim;
  let idx = gid.x + gid.y * nwg.x * 64u;
  if (idx >= total) { return; }
  let c = idx % agp.dim;
  ag_y[idx] = ag_x[idx] + ag_gate[c] * ag_delta[idx];
}

// \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500 RoPE, INTERLEAVED pairs \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
// out[2p]   = x[2p]   * cos[2p]   - x[2p+1] * sin[2p]
// out[2p+1] = x[2p+1] * cos[2p+1] + x[2p]   * sin[2p+1]
//
// cos/sin are per-TOKEN tables of head_dim entries, shared by every head, with each
// frequency REPEAT-INTERLEAVED (slots 2p and 2p+1 carry the same value) to match
// \`repeat_interleave_real=True\`. Reading cos at 2p and 2p+1 separately rather than once
// is deliberate: it keeps this kernel correct if a caller ever supplies a non-repeated
// table, and costs nothing (the value is in cache either way).
//
// \u{1F6A8} This is NOT rope_imrope.wgsl's pairing. See the header.

struct RopeP {
  tokens   : u32,
  heads    : u32,
  head_dim : u32,
  _p0 : u32,
};

@group(0) @binding(0) var<storage, read>       rp_x   : array<f32>;
@group(0) @binding(1) var<storage, read>       rp_cos : array<f32>;
@group(0) @binding(2) var<storage, read>       rp_sin : array<f32>;
@group(0) @binding(3) var<storage, read_write> rp_y   : array<f32>;
@group(0) @binding(4) var<uniform>             rpp    : RopeP;

@compute @workgroup_size(64)
fn rope_interleaved_main(@builtin(global_invocation_id) gid : vec3<u32>,
                         @builtin(num_workgroups) nwg : vec3<u32>) {
  // one thread per (token, head, PAIR)
  let pairs = rpp.head_dim / 2u;
  let total = rpp.tokens * rpp.heads * pairs;
  let idx = gid.x + gid.y * nwg.x * 64u;
  if (idx >= total) { return; }

  let pair = idx % pairs;
  let rem  = idx / pairs;
  let head = rem % rpp.heads;
  let tok  = rem / rpp.heads;

  let o = (tok * rpp.heads + head) * rpp.head_dim + pair * 2u;
  let p = tok * rpp.head_dim + pair * 2u;

  let a = rp_x[o];
  let b = rp_x[o + 1u];
  rp_y[o]      = a * rp_cos[p]      - b * rp_sin[p];
  rp_y[o + 1u] = b * rp_cos[p + 1u] + a * rp_sin[p + 1u];
}

// \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500 full (non-causal) multi-head attention \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
// \u{1F6A8} NEITHER softmax_attn.wgsl NOR softmax_attn_batched.wgsl CAN SERVE THIS MODEL, and
// the reason is not performance -- both are CAUSAL. An image transformer attends
// bidirectionally: token 3 must see token 700. Running a causal kernel here masks most
// of every row, renormalises what is left, and returns a perfectly well-formed tensor.
// The image would simply be wrong.
//
// Two more differences make the reuse impossible rather than merely incorrect: both LLM
// kernels read K/V from a 4-bit QUANTIZED KV CACHE (this model has no cache -- every
// token is present at once, in f32), and both implement GQA (this model has 24 query
// heads and 24 KV heads, so the mapping is the identity).
//
// FLASH-STYLE ONLINE SOFTMAX, one workgroup per (token, head). The running max/sum let
// it stream the key axis in tiles with no O(n^2) score buffer, which matters at 768
// tokens. Lanes split the key axis when computing scores, and split the HEAD DIM when
// accumulating the output -- so the per-lane accumulator is a couple of registers rather
// than a head_dim-wide array in workgroup memory, which at 64 lanes x 128 dims would be
// 32 KB and exceed the guaranteed limit.
//
// Layout is [token][head][dim], matching \`image/mmdit.ts attention\` -- the reference
// unflattens the projection to (heads, headDim) on the LAST axis. Reading it as
// [head][token][dim] transposes silently and is shape-compatible.

const ATT_WG : u32 = 64u;

struct AttnFullP {
  tokens   : u32,
  heads    : u32,
  head_dim : u32,
  scale    : f32,      // 1/sqrt(head_dim)
};

@group(0) @binding(0) var<storage, read>       af_q : array<f32>;
@group(0) @binding(1) var<storage, read>       af_k : array<f32>;
@group(0) @binding(2) var<storage, read>       af_v : array<f32>;
@group(0) @binding(3) var<storage, read_write> af_y : array<f32>;
@group(0) @binding(4) var<uniform>             afp  : AttnFullP;

var<workgroup> af_score : array<f32, 64>;   // one score per lane per tile
var<workgroup> af_red   : array<f32, 64>;
var<workgroup> af_m     : f32;              // running max
var<workgroup> af_l     : f32;              // running sum of exp

@compute @workgroup_size(64)
fn attn_full_main(@builtin(workgroup_id) wg : vec3<u32>,
                  @builtin(local_invocation_id) lid : vec3<u32>,
                  @builtin(num_workgroups) nwg : vec3<u32>) {
  let pair = wg.x + wg.y * nwg.x;             // (token, head), flattened
  let total = afp.tokens * afp.heads;
  if (pair >= total) { return; }
  let head = pair % afp.heads;
  let tok  = pair / afp.heads;
  let hd   = afp.head_dim;
  let lane = lid.x;

  let qo = (tok * afp.heads + head) * hd;

  if (lane == 0u) { af_m = -3.0e38; af_l = 0.0; }
  workgroupBarrier();

  // The output accumulator lives in registers: this lane owns dims lane, lane+64, ...
  // ACC_MAX bounds head_dim at 64*8 = 512; this model uses 128.
  const ACC_MAX : u32 = 8u;
  var acc : array<f32, 8>;
  for (var a : u32 = 0u; a < ACC_MAX; a = a + 1u) { acc[a] = 0.0; }

  var tile : u32 = 0u;
  loop {
    if (tile >= afp.tokens) { break; }

    // ---- scores for this tile: lane j handles key tile+lane ----
    let j = tile + lane;
    var s : f32 = -3.0e38;
    if (j < afp.tokens) {
      let ko = (j * afp.heads + head) * hd;
      var d : f32 = 0.0;
      for (var i : u32 = 0u; i < hd; i = i + 1u) { d = d + af_q[qo + i] * af_k[ko + i]; }
      s = d * afp.scale;
    }
    af_score[lane] = s;
    af_red[lane] = s;
    workgroupBarrier();

    // ---- tile max ----
    var stride : u32 = ATT_WG >> 1u;
    loop {
      if (stride == 0u) { break; }
      if (lane < stride) { af_red[lane] = max(af_red[lane], af_red[lane + stride]); }
      workgroupBarrier();
      stride = stride >> 1u;
    }
    let tile_max = af_red[0];
    workgroupBarrier();

    // ---- rescale the running state to the new max ----
    let m_old = af_m;
    let m_new = max(m_old, tile_max);
    // exp(-inf - -inf) is NaN, so guard the very first tile where both are -3e38.
    let rescale = select(exp(m_old - m_new), 0.0, m_old <= -3.0e38);
    if (lane == 0u) { af_m = m_new; }
    workgroupBarrier();

    // ---- tile sum of exp ----
    var e : f32 = 0.0;
    if (j < afp.tokens) { e = exp(af_score[lane] - m_new); }
    af_red[lane] = e;
    af_score[lane] = e;     // reuse as the weight for the accumulation below
    workgroupBarrier();
    stride = ATT_WG >> 1u;
    loop {
      if (stride == 0u) { break; }
      if (lane < stride) { af_red[lane] = af_red[lane] + af_red[lane + stride]; }
      workgroupBarrier();
      stride = stride >> 1u;
    }
    if (lane == 0u) { af_l = af_l * rescale + af_red[0]; }
    workgroupBarrier();

    // ---- accumulate weighted V over this tile, this lane's dims ----
    var a : u32 = 0u;
    loop {
      let d = lane + a * ATT_WG;
      if (d >= hd || a >= ACC_MAX) { break; }
      var sum : f32 = 0.0;
      for (var t : u32 = 0u; t < ATT_WG; t = t + 1u) {
        let kj = tile + t;
        if (kj < afp.tokens) {
          let vo = (kj * afp.heads + head) * hd;
          sum = sum + af_score[t] * af_v[vo + d];
        }
      }
      acc[a] = acc[a] * rescale + sum;
      a = a + 1u;
    }
    workgroupBarrier();

    tile = tile + ATT_WG;
  }

  let inv_l = 1.0 / af_l;
  var a2 : u32 = 0u;
  loop {
    let d = lane + a2 * ATT_WG;
    if (d >= hd || a2 >= ACC_MAX) { break; }
    af_y[qo + d] = acc[a2] * inv_l;
    a2 = a2 + 1u;
  }
}

// \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500 f32 matmul (x @ W^T) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
// y[t, o] = sum_i x[t, i] * W[o, i]   -- torch [out, in] layout, NO bias.
//
// This model has no biases anywhere, and W is stored [out, in] row-major, which makes
// the reduction contiguous in \`i\` for a fixed output. One workgroup per (token, output),
// 64 lanes splitting the K axis.
//
// f32 on purpose for the FIRST correct dispatch. The shipped weights are Q2_0 and
// q2_0_q8_0_matmul.wgsl already exists for them, but swapping it in changes the numerics
// (2-bit weights, quantized activations) so it cannot be differentially compared against
// the f32 CPU reference that proves this whole path. Correctness first, in the order this
// codebase already learned: "the transformer kernels earned their optimisations only
// after a CPU differential proved them right."

struct MmP {
  tokens : u32,
  in_dim : u32,
  out_dim : u32,
  _p0 : u32,
};

@group(0) @binding(0) var<storage, read>       mm_x : array<f32>;
@group(0) @binding(1) var<storage, read>       mm_w : array<f32>;
@group(0) @binding(2) var<storage, read_write> mm_y : array<f32>;
@group(0) @binding(3) var<uniform>             mmp  : MmP;

var<workgroup> mm_red : array<f32, 64>;

@compute @workgroup_size(64)
fn matmul_main(@builtin(workgroup_id) wg : vec3<u32>,
               @builtin(local_invocation_id) lid : vec3<u32>,
               @builtin(num_workgroups) nwg : vec3<u32>) {
  let pair = wg.x + wg.y * nwg.x;
  let total = mmp.tokens * mmp.out_dim;
  if (pair >= total) { return; }
  let o = pair % mmp.out_dim;
  let t = pair / mmp.out_dim;
  let lane = lid.x;

  var s : f32 = 0.0;
  var i : u32 = lane;
  loop {
    if (i >= mmp.in_dim) { break; }
    s = s + mm_x[t * mmp.in_dim + i] * mm_w[o * mmp.in_dim + i];
    i = i + 64u;
  }
  mm_red[lane] = s;
  workgroupBarrier();
  var stride : u32 = 32u;
  loop {
    if (stride == 0u) { break; }
    if (lane < stride) { mm_red[lane] = mm_red[lane] + mm_red[lane + stride]; }
    workgroupBarrier();
    stride = stride >> 1u;
  }
  if (lane == 0u) { mm_y[pair] = mm_red[0]; }
}

// \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500 SwiGLU, fused \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
// y[t, i] = silu(x[t, i]) * x[t, inner + i]   over a FUSED [tokens, 2*inner] input.
//
// swiglu.wgsl takes gate and up as two SEPARATE buffers. Flux2's \`linear_in\` emits both
// halves in ONE tensor, and a WebGPU bind group cannot alias two overlapping views of the
// same buffer as two read bindings -- so the split has to happen inside the kernel.
// Gate is the FIRST half; swapping the halves is dimensionally identical and wrong.

struct SgP {
  tokens : u32,
  inner  : u32,
  _p0 : u32, _p1 : u32,
};

@group(0) @binding(0) var<storage, read>       sg_x : array<f32>;
@group(0) @binding(1) var<storage, read_write> sg_y : array<f32>;
@group(0) @binding(2) var<uniform>             sgp  : SgP;

@compute @workgroup_size(64)
fn swiglu_fused_main(@builtin(global_invocation_id) gid : vec3<u32>,
                     @builtin(num_workgroups) nwg : vec3<u32>) {
  let total = sgp.tokens * sgp.inner;
  let idx = gid.x + gid.y * nwg.x * 64u;
  if (idx >= total) { return; }
  let t = idx / sgp.inner;
  let i = idx % sgp.inner;
  let base = t * sgp.inner * 2u;
  let g = sg_x[base + i];
  sg_y[idx] = (g / (1.0 + exp(-g))) * sg_x[base + sgp.inner + i];
}

// \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500 per-head RMSNorm (QK-norm) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
// y[t, h, i] = x[t, h, i] / sqrt(mean_i(x^2) + eps) * weight[i]
//
// NOT rmsnorm.wgsl, which normalises a whole row against a row-wide weight. This
// normalises EACH HEAD independently over head_dim, with a [head_dim] weight shared by
// every head \u2014 that is what \`attn.norm_q\` / \`attn.norm_k\` are in Flux2, and applying the
// row-wide kernel would mix all 24 heads into one statistic.
//
// Applied BEFORE RoPE (convention 4). One workgroup per (token, head).

struct RmsHP {
  tokens   : u32,
  heads    : u32,
  head_dim : u32,
  eps      : f32,
};

@group(0) @binding(0) var<storage, read>       rh_x : array<f32>;
@group(0) @binding(1) var<storage, read>       rh_w : array<f32>;
@group(0) @binding(2) var<storage, read_write> rh_y : array<f32>;
@group(0) @binding(3) var<uniform>             rhp  : RmsHP;

var<workgroup> rh_red : array<f32, 64>;

@compute @workgroup_size(64)
fn rmsnorm_heads_main(@builtin(workgroup_id) wg : vec3<u32>,
                      @builtin(local_invocation_id) lid : vec3<u32>,
                      @builtin(num_workgroups) nwg : vec3<u32>) {
  let pair = wg.x + wg.y * nwg.x;
  if (pair >= rhp.tokens * rhp.heads) { return; }
  let hd = rhp.head_dim;
  let base = pair * hd;          // [token][head][dim] is contiguous per (token, head)
  let lane = lid.x;

  var s : f32 = 0.0;
  var i : u32 = lane;
  loop {
    if (i >= hd) { break; }
    let v = rh_x[base + i];
    s = s + v * v;
    i = i + 64u;
  }
  rh_red[lane] = s;
  workgroupBarrier();
  var stride : u32 = 32u;
  loop {
    if (stride == 0u) { break; }
    if (lane < stride) { rh_red[lane] = rh_red[lane] + rh_red[lane + stride]; }
    workgroupBarrier();
    stride = stride >> 1u;
  }
  let inv = inverseSqrt(rh_red[0] / f32(hd) + rhp.eps);
  workgroupBarrier();

  i = lane;
  loop {
    if (i >= hd) { break; }
    rh_y[base + i] = rh_x[base + i] * inv * rh_w[i];
    i = i + 64u;
  }
}

// \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500 strided copy (gather/scatter) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
// dst[t*dst_stride + dst_off + j] = src[t*src_stride + src_off + j],  j < width
//
// \u{1F6A8} THIS EXISTS BECAUSE copyBufferToBuffer CANNOT BE RECORDED INSIDE AN OPEN COMPUTE
// PASS. The first runtime queued its slices, concatenations and de-interleaves as
// buffer copies and replayed them after \`pass.end()\` -- so every dispatch that CONSUMED
// one of those buffers read it before it had been written. The kernels were all
// individually correct on hardware and the assembled model was still wrong, diverging
// at the first double block.
//
// Splitting the compute pass at each copy would also be correct, but the single-stream
// blocks de-interleave a fused projection per token: at 768 tokens that is ~15,000 pass
// boundaries per forward. As a kernel it is one dispatch and the whole graph stays in
// one pass.
//
// One thread per (t, j). Every reshape in the MMDiT graph -- token concat, token slice,
// column range, column join -- is this op with different strides.

struct CopyP {
  tokens     : u32,
  width      : u32,
  src_stride : u32,
  src_off    : u32,
  dst_stride : u32,
  dst_off    : u32,
  _p0 : u32, _p1 : u32,
};

@group(0) @binding(0) var<storage, read>       cp_src : array<f32>;
@group(0) @binding(1) var<storage, read_write> cp_dst : array<f32>;
@group(0) @binding(2) var<uniform>             cpp    : CopyP;

@compute @workgroup_size(64)
fn copy_strided_main(@builtin(global_invocation_id) gid : vec3<u32>,
                     @builtin(num_workgroups) nwg : vec3<u32>) {
  let total = cpp.tokens * cpp.width;
  let idx = gid.x + gid.y * nwg.x * 64u;
  if (idx >= total) { return; }
  let t = idx / cpp.width;
  let j = idx % cpp.width;
  cp_dst[t * cpp.dst_stride + cpp.dst_off + j] =
    cp_src[t * cpp.src_stride + cpp.src_off + j];
}
`,qu=`// SPDX-License-Identifier: LicenseRef-Aitherium-Proprietary
// \xA9 2026 Aitherium, LLC. Original work.
// Original Aitherium WebGPU implementation \u2014 WGSL kernels ported from the PrismML
// llama.cpp fork (github.com/PrismML-Eng/llama.cpp @ prism, Aitherium/PrismML-owned).
// NO third-party Space code (HF Spaces bonsai-* explicitly excluded).
// Numerics ported from owner-owned fork: github.com/PrismML-Eng/llama.cpp @ branch "prism"
//
// 4-bit KV cache quantizer. One workgroup per (pos, kv_head) ROW; the 128 lanes cooperate
// over head_dim (DPT=2 dims/lane, head_dim=128 on every Bonsai size). Contract (matches
// reference.ts packKvRow4bit):
//     scale = roundF16(max_abs / 7)          // f16 emitted as u32 low 16 bits
//     raw   = clamp(roundAwayFromZero(x/scaleStored) + 8, 0, 15)
//     packed row = head_dim nibbles, 8 per u32, LSB-first
// The attention kernel (softmax_attn_batched) dequantizes with (f32(raw) - 8.0) * scale.
// raw 0 is unreachable (|x|/amax <= 1 so x/scale <= 7/1 after the f16 round); the clamp
// exists because scale is f16-rounded and x/scale can exceed 7 by a hair, so raw 15 (all
// ones) is the saturating ceiling for the largest magnitudes \u2014 exactly symmetric to Q8_0.
//
// Output layout per row (4-byte aligned):
//   scales[row]        : u32  \u2014 f16 scale in the LOW 16 bits
//   packed[row\xB7words + w] : u32 \u2014 8 nibbles per word, word 0 holds elements 0..7, etc.
// Requires head_dim % 8 == 0 for the flat element index -> word index (e>>3) mapping used
// by the attention kernel to be row-local, AND head_dim <= 128 because one workgroup is
// exactly 128 lanes with one dim per lane. Both asserted on the host (KvCache ctor); a
// head_dim > 128 would leave the tail of every row unquantized silently, so it must throw.

const QK4 : u32 = 8u;   // nibbles per u32
const WG4 : u32 = 128u; // lanes per row (matches head_dim on every Bonsai size)
const DPT : u32 = 2u;   // dims per lane (head_dim <= 256 asserted on the host)

struct QP {
  head_dim : u32,
  n_rows   : u32,
  row_base : u32,   // dest row offset = posBase * n_heads_kv (absolute position base)
  _p0      : u32,
};

@group(0) @binding(0) var<storage, read>       x      : array<f32>;  // n_rows * head_dim
@group(0) @binding(1) var<storage, read_write> packed : array<u32>;  // n_rows * words_per_row
@group(0) @binding(2) var<storage, read_write> scales : array<u32>;  // n_rows
@group(0) @binding(3) var<uniform>             p      : QP;

var<workgroup> shared_amax : array<f32, 128>;
// Quantized NIBBLES are exchanged as u32 (low 4 bits used). They must NOT be round-tripped
// through f32 workgroup memory: a value > 127 would be a signalling NaN bit pattern as f32
// and the GPU canonicalizes NaN on store/load (same rule as quantize_q8_0.wgsl).
var<workgroup> shared_q : array<u32, 128>;

@compute @workgroup_size(128)
fn main(@builtin(workgroup_id) wg : vec3<u32>, @builtin(local_invocation_id) lid : vec3<u32>,
        @builtin(num_workgroups) nwg : vec3<u32>) {
  // FLAT INDEX ACROSS A POSSIBLY-2D WORKGROUP GRID.
  // dispatch1D() folds the workgroup count into y once it passes WebGPU's 65535-per-dimension
  // limit. When it does not \u2014 the common case \u2014 num_workgroups.y is 1 and this reduces to
  // EXACTLY the old expression. One WORKGROUP per row, dispatched with workgroupSize=1 on the
  // host (never 128 \u2014 that would divide the group count by 128 and quantize 1/128th of rows).
  let row  = wg.x + wg.y * nwg.x;
  let lane = lid.x;
  let hd   = p.head_dim;
  if (row >= p.n_rows) { return; }

  // per-lane load + abs
  let xv = x[row * hd + lane];
  shared_amax[lane] = abs(xv);
  workgroupBarrier();

  // tree reduce max-abs across 128 lanes
  var stride : u32 = 64u;
  loop {
    if (stride == 0u) { break; }
    if (lane < stride) {
      shared_amax[lane] = max(shared_amax[lane], shared_amax[lane + stride]);
    }
    workgroupBarrier();
    stride = stride >> 1u;
  }

  let amax = shared_amax[0];
  // round scale through f16 exactly (pack/unpack) so quantization uses the stored scale \u2014
  // the attention kernel divides by THIS value, not by the full-precision amax/7.
  let scale_f16 = pack2x16float(vec2<f32>(amax / 7.0, 0.0)) & 0xffffu;
  let scale     = unpack2x16float(scale_f16).x;
  let id        = select(0.0, 1.0 / scale, scale != 0.0);

  // quantize this lane's value to a 0..15 nibble. WGSL round() rounds half away from zero,
  // which is the SAME tie rule the CPU reference implements (Math.sign*Math.round).
  let raw = clamp(round(xv * id) + 8.0, 0.0, 15.0);
  shared_q[lane] = u32(raw) & 0xFu;
  workgroupBarrier();

  if (lane == 0u) {
    let row_abs = p.row_base + row;
    scales[row_abs] = scale_f16;
    let words = (hd + QK4 - 1u) / QK4;
    for (var w : u32 = 0u; w < words; w = w + 1u) {
      var v : u32 = 0u;
      for (var k : u32 = 0u; k < QK4; k = k + 1u) {
        let idx = w * QK4 + k;
        v = v | (select(0u, shared_q[idx], idx < hd) << (k * 4u));
      }
      packed[row_abs * words + w] = v;
    }
  }
}
`,Mu=`// SPDX-License-Identifier: LicenseRef-Aitherium-Proprietary
// \xA9 2026 Aitherium, LLC. Original work.
// Original Aitherium WebGPU implementation \u2014 WGSL kernels ported from the PrismML
// llama.cpp fork (github.com/PrismML-Eng/llama.cpp @ prism, Aitherium/PrismML-owned).
// NO third-party Space code (HF Spaces bonsai-* explicitly excluded).
//
// SELECT THE TOP-K LOGITS ON THE GPU, so decode stops shipping the whole vocabulary to the
// host every single token.
//
// \u2500\u2500 WHY (measured 2026-07-31) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
// sampleToken() read back the ENTIRE logits row \u2014 vocab 248,320 x 4 B \u2248 993 KB \u2014 per token,
// then picked the top-k in JS. Once the attention kernel was parallelised, that readback
// became the single largest cost in the decode loop. Splitting the sample phase into its two
// halves settled which half, and it was not the half the code comments worried about:
//
//     sample=83.9ms  [readback=83.4ms  select=0.4ms]     (4B, 1285-token context)
//
// The JS selection pass over a quarter-million floats costs FOUR TENTHS of a millisecond.
// The transfer around it costs two hundred times that, and it scales with nothing useful \u2014
// it is the same 993 KB whether the answer is one token or a thousand. So the fix is not a
// better loop, it is to stop moving the data: select on the device and return a few hundred
// bytes. Pooling the staging buffer first was tried and did NOT move the number.
//
// \u2500\u2500 HOW, AND WHY IT IS EXACT \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
// A parallel exact top-k is awkward in WGSL (no cross-workgroup reduction primitive), and a
// per-block top-1 is NOT exact \u2014 the whole top-k can live inside one block. So: threshold,
// then gather.
//
//   pass 1 \`hist\`   \u2014 histogram every logit into NBINS bins over a FIXED logit range.
//                     Fixed, so no max-reduction pass is needed first; out-of-range values
//                     clamp into the end bins, which keeps them findable rather than lost.
//   host            \u2014 read NBINS u32 (4 KB), walk from the top bin down accumulating counts
//                     until at least K have been seen. That bin's lower edge is a threshold
//                     T with a PROVEN property: at least K logits are >= T.
//   pass 2 \`gather\` \u2014 append every (index, value) with value >= T into a compact list via an
//                     atomic counter. Read back only the counter and that list.
//
// Every logit >= T is collected, and at least K logits are >= T, so the true top-K is a
// SUBSET of what comes back. The host then does an exact top-k over a few hundred candidates
// instead of 248,320 \u2014 the same code that already cost 0.4 ms, now on a smaller input.
//
// OVERFLOW IS NOT SILENTLY WRONG. If more candidates clear T than the output can hold, the
// gather writes what fits and the counter keeps counting, so the host sees count > capacity
// and FALLS BACK to the full readback. That is slow and correct, which is the right way
// round; dropping candidates would silently change which token is sampled, and a sampling
// bug reads as the model being dumb rather than as a bug.

struct TopKP {
  vocab      : u32,
  n_bins     : u32,
  lo         : f32,   // histogram range, in logit units
  hi         : f32,
  threshold  : f32,   // gather: keep values >= this (ignored by the hist entry point)
  capacity   : u32,   // gather: max pairs the output buffers can hold
  _p0 : u32, _p1 : u32,
};

@group(0) @binding(0) var<storage, read>        logits  : array<f32>;
@group(0) @binding(1) var<storage, read_write>  hist    : array<atomic<u32>>;
@group(0) @binding(2) var<storage, read_write>  out_idx : array<u32>;
@group(0) @binding(3) var<storage, read_write>  out_val : array<f32>;
// [0] = number of candidates that cleared the threshold, INCLUDING any that did not fit.
@group(0) @binding(4) var<storage, read_write>  counter : array<atomic<u32>>;
@group(0) @binding(5) var<uniform>              p       : TopKP;

// One thread per logit. 256 is a safe workgroup size everywhere (the spec guarantees 256).
const WG : u32 = 256u;

/** Bin index for a logit value: bin 0 is the TOP of the range, so walking bins in ascending
 *  order walks logits in DESCENDING order \u2014 which is the direction the host needs. */
fn bin_of(v : f32) -> u32 {
  let span = max(p.hi - p.lo, 1e-6);
  // Fraction from the TOP of the range.
  let f = (p.hi - v) / span;
  let b = i32(floor(f * f32(p.n_bins)));
  // Clamp rather than discard: a logit above \`hi\` belongs in the top bin and a logit below
  // \`lo\` in the bottom one. Discarding out-of-range values would make the count wrong, and
  // the threshold derived from it wrong, in the one case that matters most \u2014 an unusually
  // confident token sitting above the assumed range.
  return u32(clamp(b, 0, i32(p.n_bins) - 1));
}

@compute @workgroup_size(256)
fn hist_main(@builtin(global_invocation_id) gid : vec3<u32>,
             @builtin(num_workgroups) nwg : vec3<u32>) {
  // Grid-stride, so the dispatch size does not have to divide the vocabulary and a 2-D
  // workgroup grid (dispatch1D folds past 65535) still covers every element exactly once.
  let stride = nwg.x * nwg.y * WG;
  let start = gid.x + gid.y * nwg.x * WG;
  var i = start;
  loop {
    if (i >= p.vocab) { break; }
    atomicAdd(&hist[bin_of(logits[i])], 1u);
    i = i + stride;
  }
}

@compute @workgroup_size(256)
fn gather_main(@builtin(global_invocation_id) gid : vec3<u32>,
               @builtin(num_workgroups) nwg : vec3<u32>) {
  let stride = nwg.x * nwg.y * WG;
  let start = gid.x + gid.y * nwg.x * WG;
  var i = start;
  loop {
    if (i >= p.vocab) { break; }
    let v = logits[i];
    if (v >= p.threshold) {
      // The counter is incremented even when the slot does not fit, so the host can tell
      // "collected everything" from "there were more than we could hold" and fall back.
      let slot = atomicAdd(&counter[0], 1u);
      if (slot < p.capacity) {
        out_idx[slot] = i;
        out_val[slot] = v;
      }
    }
    i = i + stride;
  }
}
`,Uu=`// SPDX-License-Identifier: LicenseRef-Aitherium-Proprietary
// \xA9 2026 Aitherium, LLC. Original work.
// Original Aitherium WebGPU implementation \u2014 WGSL kernels ported from the PrismML
// llama.cpp fork (github.com/PrismML-Eng/llama.cpp @ prism, Aitherium/PrismML-owned).
// NO third-party Space code (HF Spaces bonsai-* explicitly excluded).
// Numerics ported from owner-owned fork: github.com/PrismML-Eng/llama.cpp @ branch "prism"
//   - Q1_0 dequant ................... ggml/src/ggml-quants.c:419-437  (QK1_0=128)
//
// Standalone Q1_0 dequant for verification (Milestone 2 round-trip) and any non-hot-path
// wholesale dequant of small tensors. Contract (matches reference.ts dequantQ1Block):
//   block = { f16 d ; u8 qs[16] } = 18 bytes, 128 weights.
//   bit order LSB-first: weight j uses byte qs[j>>3], bit (j & 7).
//   bit == 1 -> +d ;  bit == 0 -> -d   (binary {-1,+1}; NOT ternary \u2014 no zero).
//
// Input packing: each 18-byte block is laid out as 5 u32 (padded) \u2014 word0 low16 = f16 d,
// bytes 2..17 = the 16 sign bytes. We pass blocks as array<u32> with 5 words per block
// (last word half-used) to stay 4-byte aligned. One thread per 128-weight block.

const QK1_0 : u32 = 128u;
const WORDS_PER_BLOCK : u32 = 5u;   // 20 bytes reserved per block (18 used)

@group(0) @binding(0) var<storage, read>       blocks : array<u32>;   // n_blocks * 5
@group(0) @binding(1) var<storage, read_write> out_w  : array<f32>;   // n_blocks * 128
@group(0) @binding(2) var<uniform>             n_blocks : u32;

fn byte_at(block_base: u32, byte_index: u32) -> u32 {
  // byte_index is 0..17 within the block; word = byte_index/4, shift = (byte_index%4)*8
  let word = blocks[block_base + (byte_index >> 2u)];
  let sh   = (byte_index & 3u) * 8u;
  return (word >> sh) & 0xffu;
}

@compute @workgroup_size(64)
fn main(@builtin(workgroup_id) wg_ : vec3<u32>,
        @builtin(local_invocation_id) lid_ : vec3<u32>,
        @builtin(num_workgroups) nwg_ : vec3<u32>) {
  // FLAT INDEX ACROSS A POSSIBLY-2D WORKGROUP GRID.
  // dispatch1D() folds the workgroup count into y once it passes WebGPU's 65535-per-dimension
  // limit. When it does not \u2014 the common case \u2014 num_workgroups.y is 1 and this reduces to
  // EXACTLY the old expression, so the working 27B numerics are untouched.
  let block = (wg_.x + wg_.y * nwg_.x) * 64u + lid_.x;
  if (block >= n_blocks) { return; }
  let bb = block * WORDS_PER_BLOCK;

  // f16 d in the low 16 bits of word 0
  let d = unpack2x16float(blocks[bb] & 0xffffu).x;

  let out_base = block * QK1_0;
  for (var j : u32 = 0u; j < QK1_0; j = j + 1u) {
    // sign bytes start at byte offset 2 within the block
    let byte = byte_at(bb, 2u + (j >> 3u));
    let bit  = (byte >> (j & 7u)) & 1u;
    out_w[out_base + j] = select(-d, d, bit == 1u);
  }
}
`,Fu=`// SPDX-License-Identifier: LicenseRef-Aitherium-Proprietary
// \xA9 2026 Aitherium, LLC. Original work.
// Original Aitherium WebGPU implementation \u2014 WGSL kernels ported from the PrismML
// llama.cpp fork (github.com/PrismML-Eng/llama.cpp @ prism, Aitherium/PrismML-owned).
// NO third-party Space code (HF Spaces bonsai-* explicitly excluded).
// Numerics ported from owner-owned fork: github.com/PrismML-Eng/llama.cpp @ branch "prism"
//   - q1_0\xB7q8_0 dot .................. ggml/src/ggml-cpu/quants.c:127-175 (ggml_vec_dot_q1_0_q8_0)
//   - Q1_0 block layout .............. ggml/src/ggml-common.h (QK1_0=128, block_q1_0)
//
// THE core kernel \u2014 reproduces ggml_vec_dot_q1_0_q8_0 EXACTLY. Binary sign selection,
// two-level scaling, integer accumulation. K-TILED with the activation row staged in
// workgroup shared memory: one workgroup owns 64 output cols of ONE row, so the row's
// activation is loaded once per K-tile and reused across all 64 cols (a 64x cut in
// activation global-memory traffic vs the scalar one-thread-per-element version). Weights
// are streamed per-col from global (sequential within a col = cache-friendly).
//
// NUMERICS ARE BIT-IDENTICAL to the scalar kernel: the f32 accumulation ORDER is preserved
// (blocks i ascending, sub-blocks k ascending; the 32-lane acc is INTEGER so order-free).
// Only memory traffic and the workgroup->(row,col) mapping changed.
//
// NON-NEGOTIABLE (verification checklist \xA710):
//   1. bit order LSB-first: weight j uses qs[j>>3], bit (j&7).
//   2. bit==1 -> +q8 ; bit==0 -> -q8  (binary, never zero).
//   3. accumulate sign-selected int8 in i32 FIRST, then * d1(per-32), sum, then * d0(per-128).
//
// Buffers:
//   weights  : array<u32> \u2014 Q1_0, 5 words/block (word0 low16 = f16 d0, bytes2..17 = signs)
//   act_d    : array<u32> \u2014 per-32 f16 activation scales d1 (low 16 bits), one per q8 block
//   act_qs   : array<u32> \u2014 per-32 int8 activations, 8 words/block (4 int8 per word)
//   out      : array<f32> \u2014 [n_rows * n_cols] output features
//   dims     : uniform {K, n_cols, n_rows, col_tiles}  col_tiles = ceil(n_cols/64)
//
// Dispatch: n_rows * col_tiles workgroups of 64 threads. workgroup wg -> row = wg/col_tiles,
// col = (wg%col_tiles)*64 + local. A workgroup NEVER straddles two rows, so the staged
// activation is unambiguous.

const QK1_0 : u32 = 128u;
const WORDS_PER_Q1 : u32 = 5u; // 20-byte GPU block (18 used + 2 pad) \u2014 matches upload.ts repack
const WORDS_PER_Q8 : u32 = 8u;
const TILE_Q1 : u32 = 32u;     // Q1_0 blocks per K-tile (32*128 = 4096 K elements)

struct Dims { K : u32, n_cols : u32, n_rows : u32, col_tiles : u32 };

@group(0) @binding(0) var<storage, read> weights : array<u32>;
@group(0) @binding(1) var<storage, read> act_d   : array<u32>;
@group(0) @binding(2) var<storage, read> act_qs  : array<u32>;
@group(0) @binding(3) var<storage, read_write> out : array<f32>;
@group(0) @binding(4) var<uniform> dims : Dims;

// Staged activation for the current K-tile (shared across all 64 cols of this workgroup's
// row). TILE_Q1 q1-blocks -> TILE_Q1*4 q8-blocks: scales + 8 words each.
var<workgroup> sh_d  : array<u32, 128>;   // TILE_Q1 * 4
var<workgroup> sh_qs : array<u32, 1024>;  // TILE_Q1 * 4 * 8

fn q1_byte(block_base: u32, byte_index: u32) -> u32 {
  let word = weights[block_base + (byte_index >> 2u)];
  return (word >> ((byte_index & 3u) * 8u)) & 0xffu;
}

fn sext8(b: u32) -> i32 {
  return (i32(b) ^ 0x80) - 0x80;
}

@compute @workgroup_size(64)
fn main(@builtin(local_invocation_id) lid : vec3<u32>,
        @builtin(workgroup_id) wid : vec3<u32>,
        @builtin(num_workgroups) nwg : vec3<u32>) {
  let local = lid.x;                 // 0..63
  // FLAT INDEX ACROSS A POSSIBLY-2D WORKGROUP GRID.
  // dispatch1D() folds the workgroup count into y once it passes WebGPU's 65535-per-dimension
  // limit. When it does not \u2014 the common case \u2014 num_workgroups.y is 1 and this reduces to
  // EXACTLY the old expression, so the working 27B numerics are untouched.
  let wg = wid.x + wid.y * nwg.x;
  let row   = wg / dims.col_tiles;   // uniform across the workgroup
  if (row >= dims.n_rows) { return; } // uniform: whole workgroup returns or none
  let col = (wg % dims.col_tiles) * 64u + local;
  let valid = col < dims.n_cols;

  let n_q1 = dims.K / QK1_0;
  let a_row_q8_base = row * (dims.K / 32u);

  var result : f32 = 0.0;

  var c0 : u32 = 0u;
  loop {
    if (c0 >= n_q1) { break; }
    let cn = min(TILE_Q1, n_q1 - c0);   // q1-blocks in this tile (uniform)
    let n_q8 = cn * 4u;                  // q8-blocks in this tile
    let q8_base = a_row_q8_base + c0 * 4u;

    // Cooperative, coalesced load of this tile's activation into shared (all 64 threads).
    var t : u32 = local;
    loop { if (t >= n_q8) { break; } sh_d[t] = act_d[q8_base + t]; t = t + 64u; }
    t = local;
    loop { if (t >= n_q8 * WORDS_PER_Q8) { break; } sh_qs[t] = act_qs[q8_base * WORDS_PER_Q8 + t]; t = t + 64u; }
    workgroupBarrier();

    if (valid) {
      var il : u32 = 0u;
      loop {
        if (il >= cn) { break; }
        let i  = c0 + il;
        let wb = (col * n_q1 + i) * WORDS_PER_Q1;
        let d0 = unpack2x16float(weights[wb] & 0xffffu).x;   // per-128 weight scale

        var block_sum : f32 = 0.0;
        for (var k : u32 = 0u; k < 4u; k = k + 1u) {
          let qb    = il * 4u + k;                              // shared q8-block index
          let d1    = unpack2x16float(sh_d[qb] & 0xffffu).x;    // per-32 activation scale
          let qs_sh = qb * WORDS_PER_Q8;

          // Hoist the 4 sign bytes for this 32-weight sub-block out of the lane loop.
          // The old q1_byte(wb, 2 + (j>>3)) was a GLOBAL weight read PER LANE \u2014 32 reads
          // that hit only 2 distinct words, re-fetched ~16x each. Weight bandwidth is the
          // decode bottleneck, so this ~8x cut on the hot path matters. Bytes 2+k*4 .. +3.
          let sbb  = 2u + k * 4u;
          let sb0  = q1_byte(wb, sbb);
          let sb1  = q1_byte(wb, sbb + 1u);
          let sb2  = q1_byte(wb, sbb + 2u);
          let sb3  = q1_byte(wb, sbb + 3u);

          var acc : i32 = 0;                                    // INTEGER accumulation (order-free)
          // Process the 32 activations as 8 words \xD7 4 int8s; sign byte = w>>1, bit = (w&1)*4+m.
          for (var wi : u32 = 0u; wi < 8u; wi = wi + 1u) {
            let aword = sh_qs[qs_sh + wi];                      // int8s from shared (one read/4 lanes)
            var sbyte = sb0;
            if (wi >= 6u) { sbyte = sb3; } else if (wi >= 4u) { sbyte = sb2; } else if (wi >= 2u) { sbyte = sb1; }
            let bitbase = (wi & 1u) * 4u;
            for (var m : u32 = 0u; m < 4u; m = m + 1u) {
              let bit = (sbyte >> (bitbase + m)) & 1u;
              let q8  = sext8((aword >> (m * 8u)) & 0xffu);
              acc = acc + select(-q8, q8, bit == 1u);
            }
          }
          block_sum = block_sum + d1 * f32(acc);               // * per-32 scale (k order)
        }
        result = result + d0 * block_sum;                      // * per-128 scale (i order)
        il = il + 1u;
      }
    }
    workgroupBarrier();                 // all threads done reading shared before next tile overwrites
    c0 = c0 + TILE_Q1;
  }

  if (valid) { out[row * dims.n_cols + col] = result; }
}
`,Ku=`// SPDX-License-Identifier: LicenseRef-Aitherium-Proprietary
// \xA9 2026 Aitherium, LLC. Original work.
// Original Aitherium WebGPU implementation \u2014 WGSL kernels ported from the PrismML
// llama.cpp fork (github.com/PrismML-Eng/llama.cpp @ prism, Aitherium/PrismML-owned).
// NO third-party Space code (HF Spaces bonsai-* explicitly excluded).
// Numerics ported from owner-owned fork: github.com/PrismML-Eng/llama.cpp @ branch "prism"
//   - Q2_0 dequant ................... ggml/src/ggml-quants.c ~450 (QK2_0=128)
//
// Standalone Q2_0 dequant for verification (Milestone 2 round-trip) and any non-hot-path
// wholesale dequant of small tensors. Contract (matches reference.ts dequantQ2Block):
//   block = { f16 d ; u8 qs[32] } = 34 bytes, 128 weights.
//   2-bit order LSB-first: weight j uses byte qs[j>>2], bits at ((j&3)<<1).
//   bit pattern (00,01,10,11) -> (\u22121,0,+1,+2) -> (\u2212d,0,+d,+2d) via formula: (q\u22121)\xB7d.
//
// Input packing: each 34-byte block is laid out as 9 u32 (padded) \u2014 word0 low16 = f16 d,
// bytes 2..33 = the 32 packed 2-bit bytes. We pass blocks as array<u32> with 9 words per block
// (last word partially used) to stay 4-byte aligned. One thread per 128-weight block.

const QK2_0 : u32 = 128u;
const WORDS_PER_BLOCK : u32 = 9u;   // 36 bytes reserved per block (34 used + 2 pad)

@group(0) @binding(0) var<storage, read>       blocks : array<u32>;   // n_blocks * 9
@group(0) @binding(1) var<storage, read_write> out_w  : array<f32>;   // n_blocks * 128
@group(0) @binding(2) var<uniform>             n_blocks : u32;

fn byte_at(block_base: u32, byte_index: u32) -> u32 {
  // byte_index is 0..33 within the block; word = byte_index/4, shift = (byte_index%4)*8
  let word = blocks[block_base + (byte_index >> 2u)];
  let sh   = (byte_index & 3u) * 8u;
  return (word >> sh) & 0xffu;
}

@compute @workgroup_size(64)
fn main(@builtin(workgroup_id) wg_ : vec3<u32>,
        @builtin(local_invocation_id) lid_ : vec3<u32>,
        @builtin(num_workgroups) nwg_ : vec3<u32>) {
  // FLAT INDEX ACROSS A POSSIBLY-2D WORKGROUP GRID.
  // dispatch1D() folds the workgroup count into y once it passes WebGPU's 65535-per-dimension
  // limit. When it does not \u2014 the common case \u2014 num_workgroups.y is 1 and this reduces to
  // EXACTLY the old expression, so the working 27B numerics are untouched.
  let block = (wg_.x + wg_.y * nwg_.x) * 64u + lid_.x;
  if (block >= n_blocks) { return; }
  let bb = block * WORDS_PER_BLOCK;

  // f16 d in the low 16 bits of word 0
  let d = unpack2x16float(blocks[bb] & 0xffffu).x;

  let out_base = block * QK2_0;
  for (var j : u32 = 0u; j < QK2_0; j = j + 1u) {
    // 2-bit bytes start at byte offset 2 within the block; 4 values per byte
    let byte_index = 2u + (j >> 2u);
    let byte = byte_at(bb, byte_index);
    // LSB-first: 2 bits at offset ((j & 3) << 1)
    let bit_offset = (j & 3u) << 1u;
    let q = (byte >> bit_offset) & 3u;
    // Dequant formula: (q - 1) * d; q \u2208 {0,1,2,3} -> {-1,0,1,2} * d
    out_w[out_base + j] = f32(i32(q) - 1) * d;
  }
}
`,Wu=`// SPDX-License-Identifier: LicenseRef-Aitherium-Proprietary
// \xA9 2026 Aitherium, LLC. Original work.
// Original Aitherium WebGPU implementation \u2014 WGSL kernels ported from the PrismML
// llama.cpp fork (github.com/PrismML-Eng/llama.cpp @ prism, Aitherium/PrismML-owned).
// NO third-party Space code (HF Spaces bonsai-* explicitly excluded).
// Numerics ported from owner-owned fork: github.com/PrismML-Eng/llama.cpp @ branch "prism"
//   - q2_0\xB7q8_0 dot .................. ggml/src/ggml-cpu/quants.c (ggml_vec_dot_q2_0_q8_0)
//   - Q2_0 block layout .............. ggml/src/ggml-common.h (QK2_0=128, block_q2_0)
//
// THE core Q2_0 kernel \u2014 reproduces ggml_vec_dot_q2_0_q8_0 EXACTLY. 2-bit dequant,
// two-level scaling, integer accumulation. K-TILED with the activation row staged in
// workgroup shared memory: one workgroup owns 64 output cols of ONE row, so the row's
// activation is loaded once per K-tile and reused across all 64 cols (a 64x cut in
// activation global-memory traffic vs the scalar one-thread-per-element version). Weights
// are streamed per-col from global (sequential within a col = cache-friendly).
//
// NUMERICS ARE BIT-IDENTICAL to the scalar kernel: the f32 accumulation ORDER is preserved
// (blocks i ascending, sub-blocks k ascending; the 32-lane acc is INTEGER so order-free).
// Only memory traffic and the workgroup->(row,col) mapping changed.
//
// NON-NEGOTIABLE (verification checklist):
//   1. 2-bit order LSB-first: weight j uses qs[j>>2], bits at ((j&3)<<1).
//   2. bit pattern (00,01,10,11) -> (-1,0,+1,+2) via formula (q-1).
//   3. accumulate (q2bit[lane] - 1) * q8[lane] in i32 FIRST, then * d1(per-32), sum, then * d0(per-128).
//
// Buffers:
//   weights  : array<u32> \u2014 Q2_0, 9 words/block (word0 low16 = f16 d0, bytes2..33 = 2-bit qs)
//   act_d    : array<u32> \u2014 per-32 f16 activation scales d1 (low 16 bits), one per q8 block
//   act_qs   : array<u32> \u2014 per-32 int8 activations, 8 words/block (4 int8 per word)
//   out      : array<f32> \u2014 [n_rows * n_cols] output features
//   dims     : uniform {K, n_cols, n_rows, col_tiles}  col_tiles = ceil(n_cols/64)
//
// Dispatch: n_rows * col_tiles workgroups of 64 threads. workgroup wg -> row = wg/col_tiles,
// col = (wg%col_tiles)*64 + local. A workgroup NEVER straddles two rows, so the staged
// activation is unambiguous.

const QK2_0 : u32 = 128u;
const WORDS_PER_Q2 : u32 = 9u; // 36-byte GPU block (34 used + 2 pad) \u2014 matches upload.ts repack
const WORDS_PER_Q8 : u32 = 8u;
const TILE_Q2 : u32 = 32u;     // Q2_0 blocks per K-tile (32*128 = 4096 K elements)

struct Dims { K : u32, n_cols : u32, n_rows : u32, col_tiles : u32 };

@group(0) @binding(0) var<storage, read> weights : array<u32>;
@group(0) @binding(1) var<storage, read> act_d   : array<u32>;
@group(0) @binding(2) var<storage, read> act_qs  : array<u32>;
@group(0) @binding(3) var<storage, read_write> out : array<f32>;
@group(0) @binding(4) var<uniform> dims : Dims;

// Staged activation for the current K-tile (shared across all 64 cols of this workgroup's
// row). TILE_Q2 q2-blocks -> TILE_Q2*4 q8-blocks: scales + 8 words each.
var<workgroup> sh_d  : array<u32, 128>;   // TILE_Q2 * 4
var<workgroup> sh_qs : array<u32, 1024>;  // TILE_Q2 * 4 * 8

fn q2_byte(block_base: u32, byte_index: u32) -> u32 {
  let word = weights[block_base + (byte_index >> 2u)];
  return (word >> ((byte_index & 3u) * 8u)) & 0xffu;
}

fn sext8(b: u32) -> i32 {
  return (i32(b) ^ 0x80) - 0x80;
}

@compute @workgroup_size(64)
fn main(@builtin(local_invocation_id) lid : vec3<u32>,
        @builtin(workgroup_id) wid : vec3<u32>,
        @builtin(num_workgroups) nwg : vec3<u32>) {
  let local = lid.x;                 // 0..63
  // FLAT INDEX ACROSS A POSSIBLY-2D WORKGROUP GRID.
  // dispatch1D() folds the workgroup count into y once it passes WebGPU's 65535-per-dimension
  // limit. When it does not \u2014 the common case \u2014 num_workgroups.y is 1 and this reduces to
  // EXACTLY the old expression, so the working 27B numerics are untouched.
  let wg = wid.x + wid.y * nwg.x;
  let row   = wg / dims.col_tiles;   // uniform across the workgroup
  if (row >= dims.n_rows) { return; } // uniform: whole workgroup returns or none
  let col = (wg % dims.col_tiles) * 64u + local;
  let valid = col < dims.n_cols;

  let n_q2 = dims.K / QK2_0;
  let a_row_q8_base = row * (dims.K / 32u);

  var result : f32 = 0.0;

  var c0 : u32 = 0u;
  loop {
    if (c0 >= n_q2) { break; }
    let cn = min(TILE_Q2, n_q2 - c0);   // q2-blocks in this tile (uniform)
    let n_q8 = cn * 4u;                  // q8-blocks in this tile
    let q8_base = a_row_q8_base + c0 * 4u;

    // Cooperative, coalesced load of this tile's activation into shared (all 64 threads).
    var t : u32 = local;
    loop { if (t >= n_q8) { break; } sh_d[t] = act_d[q8_base + t]; t = t + 64u; }
    t = local;
    loop { if (t >= n_q8 * WORDS_PER_Q8) { break; } sh_qs[t] = act_qs[q8_base * WORDS_PER_Q8 + t]; t = t + 64u; }
    workgroupBarrier();

    if (valid) {
      var il : u32 = 0u;
      loop {
        if (il >= cn) { break; }
        let i  = c0 + il;
        let wb = (col * n_q2 + i) * WORDS_PER_Q2;
        let d0 = unpack2x16float(weights[wb] & 0xffffu).x;   // per-128 weight scale

        var block_sum : f32 = 0.0;
        for (var k : u32 = 0u; k < 4u; k = k + 1u) {
          let qb    = il * 4u + k;                              // shared q8-block index
          let d1    = unpack2x16float(sh_d[qb] & 0xffffu).x;    // per-32 activation scale
          let qs_sh = qb * WORDS_PER_Q8;

          // Q2_0 packing: 32 weights in 8 bytes (4 weights per byte, 2 bits each).
          // Hoist the 8 packed bytes for this 32-weight sub-block to avoid per-lane reads.
          // Bytes 2 + k*8 .. +7 (8 bytes per 32-lane sub-block, LSB-first 2-bit order).
          let sbb  = 2u + k * 8u;
          let sb0  = q2_byte(wb, sbb);
          let sb1  = q2_byte(wb, sbb + 1u);
          let sb2  = q2_byte(wb, sbb + 2u);
          let sb3  = q2_byte(wb, sbb + 3u);
          let sb4  = q2_byte(wb, sbb + 4u);
          let sb5  = q2_byte(wb, sbb + 5u);
          let sb6  = q2_byte(wb, sbb + 6u);
          let sb7  = q2_byte(wb, sbb + 7u);

          var acc : i32 = 0;                                    // INTEGER accumulation (order-free)
          // Process the 32 activations as 8 words \xD7 4 int8s. Each lane j within the 32 spans
          // 2 bits at byte (j>>2), offset ((j&3)<<1).
          for (var wi : u32 = 0u; wi < 8u; wi = wi + 1u) {
            let aword = sh_qs[qs_sh + wi];                      // int8s from shared (one read/4 lanes)
            var sbyte = sb0;
            if (wi == 1u) { sbyte = sb1; }
            else if (wi == 2u) { sbyte = sb2; }
            else if (wi == 3u) { sbyte = sb3; }
            else if (wi == 4u) { sbyte = sb4; }
            else if (wi == 5u) { sbyte = sb5; }
            else if (wi == 6u) { sbyte = sb6; }
            else if (wi == 7u) { sbyte = sb7; }
            // Extract 4 2-bit values from sbyte and their paired q8 activations.
            for (var m : u32 = 0u; m < 4u; m = m + 1u) {
              let bit_offset = m << 1u;  // 2 bits at ((m & 3) << 1)
              let q2 = (sbyte >> bit_offset) & 3u;  // extract 2-bit value
              let q8 = sext8((aword >> (m * 8u)) & 0xffu);
              acc = acc + (i32(q2) - 1) * q8;  // formula: (q - 1) * a8
            }
          }
          block_sum = block_sum + d1 * f32(acc);               // * per-32 scale (k order)
        }
        result = result + d0 * block_sum;                      // * per-128 scale (i order)
        il = il + 1u;
      }
    }
    workgroupBarrier();                 // all threads done reading shared before next tile overwrites
    c0 = c0 + TILE_Q2;
  }

  if (valid) { out[row * dims.n_cols + col] = result; }
}
`,ju=`// SPDX-License-Identifier: LicenseRef-Aitherium-Proprietary
// \xA9 2026 Aitherium, LLC. Original work.
// Original Aitherium WebGPU implementation \u2014 WGSL kernels ported from the PrismML
// llama.cpp fork (github.com/PrismML-Eng/llama.cpp @ prism, Aitherium/PrismML-owned).
// NO third-party Space code (HF Spaces bonsai-* explicitly excluded).
// Numerics ported from owner-owned fork: github.com/PrismML-Eng/llama.cpp @ branch "prism"
//   - Q8_0 quant ..................... ggml/src/ggml-quants.c (quantize_row_q8_0, QK8_0=32)
//
// Activation quantizer. One workgroup per 32-element block. Contract (matches
// reference.ts quantizeQ8Block):  d = max(|x|)/127 ;  qs[j] = round(x[j]/d) clamped
// [-127,127] ;  d==0 -> qs=0.  d is emitted as f16 bits so the matmul reads exactly the
// value the CPU reference rounds to.
//
// Output layout per block (kept 4-byte aligned): one u32 for the f16 d (low 16 bits),
// then 8 u32 packing the 32 signed int8 qs (4 per u32, little-endian byte order).

const QK8_0 : u32 = 32u;

@group(0) @binding(0) var<storage, read>        activations : array<f32>;   // n_blocks * 32
@group(0) @binding(1) var<storage, read_write>  out_d       : array<u32>;    // n_blocks (f16 in low 16)
@group(0) @binding(2) var<storage, read_write>  out_qs      : array<u32>;    // n_blocks * 8

var<workgroup> shared_amax : array<f32, 32>;
// Quantized int8 values are exchanged as INTEGERS (low 8 bits used). They must NOT be
// round-tripped through f32 workgroup memory: a negative int8's bit pattern is a NaN as
// f32, and the GPU canonicalizes NaN on store/load, corrupting every negative activation
// to 0x7FC00000 (\u22482.1e9) \u2014 which then blows the matmul up ~160,000\xD7. Use a u32 scratch.
var<workgroup> shared_q : array<u32, 32>;

@compute @workgroup_size(32)
fn main(@builtin(workgroup_id) wg : vec3<u32>, @builtin(local_invocation_id) lid : vec3<u32>,
        @builtin(num_workgroups) nwg : vec3<u32>) {
  // FLAT INDEX ACROSS A POSSIBLY-2D WORKGROUP GRID.
  // dispatch1D() folds the workgroup count into y once it passes WebGPU's 65535-per-dimension
  // limit. When it does not \u2014 the common case \u2014 num_workgroups.y is 1 and this reduces to
  // EXACTLY the old expression, so the working 27B numerics are untouched.
  let block = wg.x + wg.y * nwg.x;
  let lane  = lid.x;
  let base  = block * QK8_0;

  // per-lane load + abs
  let x = activations[base + lane];
  shared_amax[lane] = abs(x);
  workgroupBarrier();

  // tree reduce max-abs across 32 lanes
  var stride : u32 = 16u;
  loop {
    if (stride == 0u) { break; }
    if (lane < stride) {
      shared_amax[lane] = max(shared_amax[lane], shared_amax[lane + stride]);
    }
    workgroupBarrier();
    stride = stride >> 1u;
  }

  let amax = shared_amax[0];
  // round d through f16 exactly (pack/unpack) so quantization uses the stored scale
  let d_f16 = pack2x16float(vec2<f32>(amax / 127.0, 0.0)) & 0xffffu;
  let d     = unpack2x16float(d_f16).x;
  let id    = select(0.0, 1.0 / d, d != 0.0);

  // quantize this lane's value; keep the low 8 bits (two's-complement int8) in a u32
  var q : i32 = i32(round(x * id));
  q = clamp(q, -127, 127);

  // Exchange the 32 quantized bytes via the INTEGER scratch (no f32/NaN round-trip).
  shared_q[lane] = u32(q) & 0xffu;
  workgroupBarrier();

  if (lane == 0u) {
    out_d[block] = d_f16;
    for (var w : u32 = 0u; w < 8u; w = w + 1u) {
      let o = w * 4u;
      out_qs[block * 8u + w] =
          shared_q[o + 0u]
        | (shared_q[o + 1u] << 8u)
        | (shared_q[o + 2u] << 16u)
        | (shared_q[o + 3u] << 24u);
    }
  }
}
`,Qu=`// SPDX-License-Identifier: LicenseRef-Aitherium-Proprietary
// \xA9 2026 Aitherium, LLC. Original work.
// Original Aitherium WebGPU implementation \u2014 WGSL kernels ported from the PrismML
// llama.cpp fork (github.com/PrismML-Eng/llama.cpp @ prism, Aitherium/PrismML-owned).
// NO third-party Space code (HF Spaces bonsai-* explicitly excluded).
// Numerics ported from owner-owned fork: github.com/PrismML-Eng/llama.cpp @ branch "prism"
//   - RMSNorm ........................ y = x / sqrt(mean(x^2)+eps) * w ; eps from GGUF KV
//
// One workgroup per row; two-pass reduce (sum of squares -> normalize). f32 accumulation
// regardless of f16 storage. Matches reference.ts rmsnorm.

struct Params { n : u32, eps : f32, _p0 : u32, _p1 : u32 };

@group(0) @binding(0) var<storage, read>       x      : array<f32>;   // n_rows * n
@group(0) @binding(1) var<storage, read>       weight : array<f32>;   // n
@group(0) @binding(2) var<storage, read_write> y      : array<f32>;   // n_rows * n
@group(0) @binding(3) var<uniform>             params : Params;

const WG : u32 = 256u;
var<workgroup> partial : array<f32, WG>;

@compute @workgroup_size(WG)
fn main(@builtin(workgroup_id) wg : vec3<u32>, @builtin(local_invocation_id) lid : vec3<u32>,
        @builtin(num_workgroups) nwg : vec3<u32>) {
  // FLAT INDEX ACROSS A POSSIBLY-2D WORKGROUP GRID.
  // dispatch1D() folds the workgroup count into y once it passes WebGPU's 65535-per-dimension
  // limit. When it does not \u2014 the common case \u2014 num_workgroups.y is 1 and this reduces to
  // EXACTLY the old expression, so the working 27B numerics are untouched.
  let row = wg.x + wg.y * nwg.x;
  let n    = params.n;
  let base = row * n;
  let tid  = lid.x;

  var ss : f32 = 0.0;
  var i : u32 = tid;
  loop {
    if (i >= n) { break; }
    let v = x[base + i];
    ss = ss + v * v;
    i = i + WG;
  }
  partial[tid] = ss;
  workgroupBarrier();

  var stride : u32 = WG >> 1u;
  loop {
    if (stride == 0u) { break; }
    if (tid < stride) { partial[tid] = partial[tid] + partial[tid + stride]; }
    workgroupBarrier();
    stride = stride >> 1u;
  }

  let mean = partial[0] / f32(n);
  let scale = inverseSqrt(mean + params.eps);

  var o : u32 = tid;
  loop {
    if (o >= n) { break; }
    y[base + o] = x[base + o] * scale * weight[o];
    o = o + WG;
  }
}
`,zu=`// SPDX-License-Identifier: LicenseRef-Aitherium-Proprietary
// \xA9 2026 Aitherium, LLC. Original work.
// Original Aitherium WebGPU implementation \u2014 WGSL kernels ported from the PrismML
// llama.cpp fork (github.com/PrismML-Eng/llama.cpp @ prism, Aitherium/PrismML-owned).
// NO third-party Space code (HF Spaces bonsai-* explicitly excluded).
// Numerics ported from owner-owned fork: github.com/PrismML-Eng/llama.cpp @ branch "prism"
//   - IMROPE ......................... src/llama-model.cpp:2494-2496 (interleaved, NOT NEOX)
//
// Interleaved multimodal RoPE. "Interleaved" = the t/h/w SECTION cycling per pair
// (all equal for text), NOT component pairing. Pairing is NEOX-style (p, p+rot/2) \u2014
// ggml routes GGML_ROPE_TYPE_IMROPE through rotate_pairs(n_dims, n_dims/2).
// theta_base from qwen35.rope.freq_base,
// rotary width from qwen35.rope.dimension_count. Applied to Q and K after projection,
// before attention.  NOTE (\xA78 risk #5): the interleaved index mapping is a common port
// bug \u2014 the golden-vector test (Milestone 4) pins this against a fork-derived reference.
//
// Pairing used here (fork-verified 2026-07-22): pair p touches components
// (p, p+rot/2). The freq for pair p is theta = pos * freq_base^(-2p/rot).

struct RopeP {
  n_heads   : u32,
  head_dim  : u32,
  rot_dim   : u32,   // rope.dimension_count (<= head_dim)
  pos_base  : u32,   // position of the first token in this batch
  freq_base : f32,
  scale     : f32,   // linear rope scaling factor (1.0 = none)
  _p0 : u32, _p1 : u32,
};

@group(0) @binding(0) var<storage, read_write> data : array<f32>;   // [n_tokens * n_heads * head_dim]
@group(0) @binding(1) var<uniform>             p    : RopeP;

@compute @workgroup_size(64)
fn main(@builtin(workgroup_id) wg_ : vec3<u32>,
        @builtin(local_invocation_id) lid_ : vec3<u32>,
        @builtin(num_workgroups) nwg_ : vec3<u32>) {
  // one thread per (token, head, pair)
  let pairs_per_head = p.rot_dim / 2u;
  let per_token = p.n_heads * pairs_per_head;
  // FLAT INDEX ACROSS A POSSIBLY-2D WORKGROUP GRID.
  // dispatch1D() folds the workgroup count into y once it passes WebGPU's 65535-per-dimension
  // limit. When it does not \u2014 the common case \u2014 num_workgroups.y is 1 and this reduces to
  // EXACTLY the old expression, so the working 27B numerics are untouched.
  let idx = (wg_.x + wg_.y * nwg_.x) * 64u + lid_.x;

  let token = idx / per_token;
  let rem   = idx % per_token;
  let head  = rem / pairs_per_head;
  let pair  = rem % pairs_per_head;

  let head_base = (token * p.n_heads + head) * p.head_dim;
  // NEOX-style pairing (p, p + rot/2): ggml routes GGML_ROPE_TYPE_IMROPE through
  // rotate_pairs(n_dims, n_dims/2) \u2014 the "interleaved" in IMROPE is the t/h/w SECTION
  // cycling, NOT component pairing. For text all sections carry the same position
  // (e-stream unused: sections [11,11,10,0] cover all 32 pairs), so pairing is the
  // ONLY layout difference. The old (2p, 2p+1) pairing scrambled positional phase.
  let i0 = head_base + pair;            // (p, p + rot/2)
  let i1 = i0 + pairs_per_head;

  let pos   = f32(p.pos_base + token) * p.scale;
  let exponent = -2.0 * f32(pair) / f32(p.rot_dim);
  let theta = pos * pow(p.freq_base, exponent);
  let c = cos(theta);
  let s = sin(theta);

  let x0 = data[i0];
  let x1 = data[i1];
  data[i0] = x0 * c - x1 * s;
  data[i1] = x0 * s + x1 * c;
}
`,Hu=`// SPDX-License-Identifier: LicenseRef-Aitherium-Proprietary
// \xA9 2026 Aitherium, LLC. Original work.
// Original Aitherium WebGPU implementation \u2014 WGSL kernels ported from the PrismML
// llama.cpp fork (github.com/PrismML-Eng/llama.cpp @ prism, Aitherium/PrismML-owned).
// NO third-party Space code (HF Spaces bonsai-* explicitly excluded).
// Numerics ported from owner-owned fork: github.com/PrismML-Eng/llama.cpp @ branch "prism"
//   - temperature / top-k / top-p sampling (card defaults temp 0.7, top-k 20, top-p 0.95)
//
// v1 strategy: this kernel computes the argmax fast path (temp ~ 0) and a temperature-
// scaled max for numerical stability; full top-k/top-p nucleus truncation is done on the
// host over the reduced candidate set for v1 (simpler + exact), with a GPU bitonic top-k
// as the follow-up optimisation. Runs over the final logits row (~151K vocab).

struct SampleP { vocab : u32, temperature : f32, _p0 : u32, _p1 : u32 };

@group(0) @binding(0) var<storage, read>       logits  : array<f32>;   // [vocab]
@group(0) @binding(1) var<storage, read_write> argmax  : array<u32>;   // [1] best token id
@group(0) @binding(2) var<storage, read_write> maxval  : array<f32>;   // [1] max logit
@group(0) @binding(3) var<uniform>             p       : SampleP;

const WG : u32 = 256u;
var<workgroup> best_val : array<f32, WG>;
var<workgroup> best_idx : array<u32, WG>;

@compute @workgroup_size(WG)
fn main(@builtin(local_invocation_id) lid : vec3<u32>) {
  let tid = lid.x;
  var bv : f32 = -3.0e38;
  var bi : u32 = 0u;
  var i : u32 = tid;
  loop {
    if (i >= p.vocab) { break; }
    let l = logits[i];
    if (l > bv) { bv = l; bi = i; }
    i = i + WG;
  }
  best_val[tid] = bv;
  best_idx[tid] = bi;
  workgroupBarrier();

  var stride : u32 = WG >> 1u;
  loop {
    if (stride == 0u) { break; }
    if (tid < stride) {
      if (best_val[tid + stride] > best_val[tid]) {
        best_val[tid] = best_val[tid + stride];
        best_idx[tid] = best_idx[tid + stride];
      }
    }
    workgroupBarrier();
    stride = stride >> 1u;
  }

  if (tid == 0u) {
    argmax[0] = best_idx[0];
    maxval[0] = best_val[0];
  }
}
`,Yu=`// SPDX-License-Identifier: LicenseRef-Aitherium-Proprietary
// \xA9 2026 Aitherium, LLC. Original work.
// Original Aitherium WebGPU implementation \u2014 WGSL kernels ported from the PrismML
// llama.cpp fork (github.com/PrismML-Eng/llama.cpp @ prism, Aitherium/PrismML-owned).
// NO third-party Space code (HF Spaces bonsai-* explicitly excluded).
// Numerics ported from owner-owned fork: github.com/PrismML-Eng/llama.cpp @ branch "prism"
//   - scaled-dot-product attention with causal mask + GQA (head_count / head_count_kv).
//
// Full-attention layers (16 of 64). Online (flash-style) softmax to bound memory over
// long context. One workgroup per (query token, query head). K/V read from the 4-bit KV
// cache and dequantized inline (see kvcache.ts / elementwise KV unpack helpers).
// v1: f32 K/V input path (dequant done host/pre-pass); 4-bit inline unpack is a follow-up.

struct AttnP {
  head_dim   : u32,
  n_kv       : u32,   // number of cached keys (context length so far)
  q_head     : u32,   // this query head index
  kv_head    : u32,   // mapped KV head (GQA: q_head / (n_head/n_head_kv))
  scale      : f32,   // 1/sqrt(head_dim)
  _p0 : u32, _p1 : u32, _p2 : u32,
};

@group(0) @binding(0) var<storage, read>       q  : array<f32>;   // [head_dim] for this query
@group(0) @binding(1) var<storage, read>       k  : array<f32>;   // [n_kv * head_dim]
@group(0) @binding(2) var<storage, read>       v  : array<f32>;   // [n_kv * head_dim]
@group(0) @binding(3) var<storage, read_write> out : array<f32>;  // [head_dim]
@group(0) @binding(4) var<uniform>             p   : AttnP;

@compute @workgroup_size(1)
fn main() {
  let hd = p.head_dim;

  // online softmax accumulators
  var m : f32 = -3.0e38;             // running max
  var l : f32 = 0.0;                 // running denom
  var acc : array<f32, 256>;         // running weighted V (head_dim <= 256)
  for (var d : u32 = 0u; d < hd; d = d + 1u) { acc[d] = 0.0; }

  for (var t : u32 = 0u; t < p.n_kv; t = t + 1u) {
    // score = scale * dot(q, k_t)
    var s : f32 = 0.0;
    let kb = t * hd;
    for (var d : u32 = 0u; d < hd; d = d + 1u) { s = s + q[d] * k[kb + d]; }
    s = s * p.scale;

    let m_new = max(m, s);
    let correction = exp(m - m_new);
    let w = exp(s - m_new);
    l = l * correction + w;
    let vb = t * hd;
    for (var d : u32 = 0u; d < hd; d = d + 1u) {
      acc[d] = acc[d] * correction + w * v[vb + d];
    }
    m = m_new;
  }

  let inv = select(0.0, 1.0 / l, l > 0.0);
  for (var d : u32 = 0u; d < hd; d = d + 1u) { out[d] = acc[d] * inv; }
}
`,Xu=`// SPDX-License-Identifier: LicenseRef-Aitherium-Proprietary
// \xA9 2026 Aitherium, LLC. Original work.
// Original Aitherium WebGPU implementation \u2014 WGSL kernels ported from the PrismML
// llama.cpp fork (github.com/PrismML-Eng/llama.cpp @ prism, Aitherium/PrismML-owned).
// NO third-party Space code (HF Spaces bonsai-* explicitly excluded).
//
// Batched causal GQA softmax attention \u2014 the WHOLE (token \xD7 head) grid in ONE dispatch,
// reading Q/K/V straight from the resident buffers. Replaces the per-(token,head) host loop
// that submitted ~n_tokens\xB7n_heads\xB73 GPU commands per layer (the dominant prefill cost).
// One WORKGROUP per (query token, query head); online (flash-style) softmax over the causal
// key range. GQA maps each query head to kv_head = q_head / (n_heads / n_heads_kv).
//
//   q       : [n_tokens \xB7 n_heads   \xB7 head_dim]   (this batch's queries, post-RoPE)
//   k_cache : [kv_len   \xB7 n_heads_kv \xB7 head_dim]  (all keys so far, incl. this batch)
//   v_cache : [kv_len   \xB7 n_heads_kv \xB7 head_dim]
//   out     : [n_tokens \xB7 n_heads   \xB7 head_dim]
// Causal: query at absolute position (pos_base + t) attends to cache positions [0, pos_base+t].
//
// \u2500\u2500 WHY THIS IS PARALLEL OVER head_dim (measured 2026-07-31) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
// This kernel was \`@workgroup_size(1)\`: ONE GPU thread per (token, head), walking the entire
// KV cache serially and reading head_dim floats one at a time. For a DECODE step n_tokens is
// 1, so the whole dispatch was n_heads threads \u2014 32 of a 5090's 21,760 lanes \u2014 each doing
// kv_len\xB7head_dim\xB72 serial scalar ops, per layer, per token. Cost was therefore LINEAR in
// context length with a ~1-lane constant, and every read was strided by head_dim (one lane
// touching a whole cache line and using 4 bytes of it).
//
// That is invisible until the prompt grows. Measured on Bonsai-4B, same box, same session:
//
//     prompt tokens   forward/token   tok/s
//     20              111 ms          7.6
//     169             ~128 ms         7.8
//     1285            646 ms          1.0        <- the shipped greeter prompt
//
// The greeter sends its framing plus getToolDefinitions() \u2014 1290 tokens \u2014 so aitherium.com
// visitors were getting ~1 tok/s while the microbenchmark (a 20-token prompt) reported 7.6
// and the engine was blamed. NOTHING regressed in this file; the prompt crossed the point
// where an O(kv_len) single-lane loop dominates the 545 MB of weight matmuls around it.
//
// So: one workgroup per (token, head), WG threads cooperating over head_dim.
//   - thread \`tid\` owns dims {tid, tid+WG, \u2026}, keeping q and the output accumulator in
//     REGISTERS (never workgroup storage \u2014 head_dim\xB7WG floats would blow the 16 KB
//     guaranteed workgroup-storage limit; only the WG-float reduction scratch lives there).
//   - at each position the q\xB7k dot product is a tree reduction across the workgroup, so
//     adjacent threads read ADJACENT k_cache/v_cache elements \u2014 coalesced, one cache line
//     serving the whole warp instead of one lane.
// The position loop stays serial and in the same order, which is what keeps the online
// softmax exact; only the dot product's summation order changes (sequential -> tree), and a
// tree reduction is no less accurate than the sequential sum it replaces. Correctness is
// gated by the whole-model GPU-vs-CPU differential in selftest/, which requires argmax
// agreement \u2014 an attention bug corrupts every downstream logit and shows up there.

struct BAttnP {
  n_tokens   : u32,
  n_heads    : u32,
  n_heads_kv : u32,
  head_dim   : u32,
  pos_base   : u32,   // absolute position of this batch's first token
  scale      : f32,   // 1/sqrt(head_dim)
  mode : u32, _p1 : u32,   // mode: 0 = f32 cache (default), 1 = 4-bit packed cache
};

@group(0) @binding(0) var<storage, read>       q          : array<f32>;
@group(0) @binding(1) var<storage, read>       k_cache    : array<u32>;
@group(0) @binding(2) var<storage, read>       v_cache    : array<u32>;
@group(0) @binding(3) var<storage, read_write> out        : array<f32>;
@group(0) @binding(4) var<uniform>             p          : BAttnP;
// 4-bit mode only (mode==1): per-(pos,kv_head) f16 scales, one u32 per row (f16 in low 16
// bits). In f32 mode these are 4-byte DUMMY buffers, always bound but NEVER indexed \u2014 the
// uniform \`if (p.mode == 1u)\` guard is what keeps them unread, because \`select()\` would
// evaluate both operands and index them OOB at large positions.
@group(0) @binding(5) var<storage, read> k_scale_buf : array<u32>;
@group(0) @binding(6) var<storage, read> v_scale_buf : array<u32>;

// 4-bit dequant read. mode==1: element e (row-aligned, head_dim%8==0 asserted on the host)
// is a NIBBLE: word e>>3, nibble e&7, value (raw-8)*scale. mode==0: the buffer holds raw
// f32 bytes and bitcast reinterprets them \u2014 byte-identical to the historical array<f32>
// binding. The scale is passed in, never re-fetched here.
fn readK(mode : u32, e : u32, scale : f32) -> f32 {
  if (mode == 1u) {
    let w = e >> 3u;
    let n = e & 7u;
    let raw = (k_cache[w] >> (n * 4u)) & 0xFu;
    return (f32(raw) - 8.0) * scale;
  }
  return bitcast<f32>(k_cache[e]);
}
fn readV(mode : u32, e : u32, scale : f32) -> f32 {
  if (mode == 1u) {
    let w = e >> 3u;
    let n = e & 7u;
    let raw = (v_cache[w] >> (n * 4u)) & 0xFu;
    return (f32(raw) - 8.0) * scale;
  }
  return bitcast<f32>(v_cache[e]);
}

// 128 lanes: WebGPU GUARANTEES maxComputeInvocationsPerWorkgroup >= 256 and
// maxComputeWorkgroupSizeX >= 256, so this is portable, and it equals head_dim on every
// Bonsai size (1.7B/4B/8B/27B all use 128) \u2014 i.e. exactly one dim per lane, no tail.
const WG : u32 = 128u;
// head_dim <= 256 (asserted by the host), so at most 2 dims per lane.
const DPT : u32 = 2u;

// Reduction scratch: WG floats = 512 bytes, far under the 16 KB guaranteed limit.
var<workgroup> red : array<f32, 128>;

@compute @workgroup_size(128)
fn main(@builtin(workgroup_id) wg_ : vec3<u32>,
        @builtin(local_invocation_id) lid_ : vec3<u32>,
        @builtin(num_workgroups) nwg_ : vec3<u32>) {
  // FLAT INDEX ACROSS A POSSIBLY-2D WORKGROUP GRID.
  // dispatch1D() folds the workgroup count into y once it passes WebGPU's 65535-per-dimension
  // limit. When it does not \u2014 the common case \u2014 num_workgroups.y is 1 and this reduces to
  // wg_.x. The index is now the WORKGROUP's, not the invocation's: the whole workgroup
  // cooperates on one (token, head).
  let idx = wg_.x + wg_.y * nwg_.x;
  let total = p.n_tokens * p.n_heads;
  // UNIFORM across the workgroup (it depends only on workgroup_id), so returning here before
  // the barriers below is legal \u2014 a non-uniform early return would be undefined behaviour.
  if (idx >= total) { return; }

  let tid = lid_.x;
  let hd  = p.head_dim;
  let t   = idx / p.n_heads;         // query token in this batch
  let h   = idx % p.n_heads;         // query head
  let kv_head = h / (p.n_heads / p.n_heads_kv);

  let q_base = (t * p.n_heads + h) * hd;
  let kv_per_pos = p.n_heads_kv * hd;
  let last = p.pos_base + t;         // inclusive causal limit

  // This lane's slice of q and of the output accumulator, held in registers.
  var qv  : array<f32, 2>;
  var acc : array<f32, 2>;
  for (var i : u32 = 0u; i < DPT; i = i + 1u) {
    let d = tid + i * WG;
    qv[i]  = select(0.0, q[q_base + d], d < hd);
    acc[i] = 0.0;
  }

  // online softmax accumulators \u2014 identical algebra to the scalar version, and every lane
  // carries the same m/l because they all consume the same reduced score.
  var m : f32 = -3.0e38;
  var l : f32 = 0.0;

  for (var pos : u32 = 0u; pos <= last; pos = pos + 1u) {
    // Per-(pos,kv_head) f16 scales, fetched ONCE per position. The \`if\` is a uniform branch
    // (the same value for every lane in the workgroup), so it cannot diverge a barrier; it
    // is deliberately NOT a \`select()\`, which would read the dummy 4-byte scale buffer OOB
    // in f32 mode once sIdx grows past element 0.
    var kScale : f32 = 0.0;
    var vScale : f32 = 0.0;
    if (p.mode == 1u) {
      let sIdx = pos * p.n_heads_kv + kv_head;
      kScale = unpack2x16float(k_scale_buf[sIdx]).x;
      vScale = unpack2x16float(v_scale_buf[sIdx]).x;
    }
    let k_base = pos * kv_per_pos + kv_head * hd;
    var part : f32 = 0.0;
    for (var i : u32 = 0u; i < DPT; i = i + 1u) {
      let d = tid + i * WG;
      if (d < hd) { part = part + qv[i] * readK(p.mode, k_base + d, kScale); }
    }
    red[tid] = part;
    workgroupBarrier();

    // Tree reduction. The barrier is OUTSIDE the \`if\`, because a barrier inside non-uniform
    // control flow is undefined behaviour; the trip count is a constant so every lane runs
    // the same number of iterations.
    var stride : u32 = WG / 2u;
    loop {
      if (stride == 0u) { break; }
      if (tid < stride) { red[tid] = red[tid] + red[tid + stride]; }
      workgroupBarrier();
      stride = stride / 2u;
    }

    let s = red[0] * p.scale;

    let m_new = max(m, s);
    let corr  = exp(m - m_new);
    let w     = exp(s - m_new);
    l = l * corr + w;
    let v_base = pos * kv_per_pos + kv_head * hd;
    for (var i : u32 = 0u; i < DPT; i = i + 1u) {
      let d = tid + i * WG;
      if (d < hd) { acc[i] = acc[i] * corr + w * readV(p.mode, v_base + d, vScale); }
    }
    m = m_new;

    // Every lane has now READ red[0]; without this the next iteration's \`red[tid] = part\`
    // could overwrite it while a slower lane is still reading. Silent wrong scores, not a
    // crash \u2014 the failure mode this whole file exists to avoid.
    workgroupBarrier();
  }

  let inv = select(0.0, 1.0 / l, l > 0.0);
  for (var i : u32 = 0u; i < DPT; i = i + 1u) {
    let d = tid + i * WG;
    if (d < hd) { out[q_base + d] = acc[i] * inv; }
  }
}
`,Vu=`// SPDX-License-Identifier: LicenseRef-Aitherium-Proprietary
// \xA9 2026 Aitherium, LLC. Original work.
// Original Aitherium WebGPU implementation \u2014 WGSL kernels ported from the PrismML
// llama.cpp fork (github.com/PrismML-Eng/llama.cpp @ prism, Aitherium/PrismML-owned).
// NO third-party Space code (HF Spaces bonsai-* explicitly excluded).
// Numerics ported from owner-owned fork: github.com/PrismML-Eng/llama.cpp @ branch "prism"
//   - SwiGLU ......................... down( silu(gate(x)) * up(x) ), silu(z)=z*sigmoid(z)
//
// This kernel is ONLY the element-wise silu(gate)*up stage; gate/up/down are Q1_0 matmuls
// (q1_0_q8_0_matmul.wgsl). Matches reference.ts swigluMul.

@group(0) @binding(0) var<storage, read>       gate : array<f32>;
@group(0) @binding(1) var<storage, read>       up   : array<f32>;
@group(0) @binding(2) var<storage, read_write> out  : array<f32>;
@group(0) @binding(3) var<uniform>             n    : u32;

fn silu(z : f32) -> f32 { return z / (1.0 + exp(-z)); }

@compute @workgroup_size(256)
fn main(@builtin(workgroup_id) wg_ : vec3<u32>,
        @builtin(local_invocation_id) lid_ : vec3<u32>,
        @builtin(num_workgroups) nwg_ : vec3<u32>) {
  // FLAT INDEX ACROSS A POSSIBLY-2D WORKGROUP GRID.
  // dispatch1D() folds the workgroup count into y once it passes WebGPU's 65535-per-dimension
  // limit. When it does not \u2014 the common case \u2014 num_workgroups.y is 1 and this reduces to
  // EXACTLY the old expression, so the working 27B numerics are untouched.
  let i = (wg_.x + wg_.y * nwg_.x) * 256u + lid_.x;
  if (i >= n) { return; }
  out[i] = silu(gate[i]) * up[i];
}
`,Ju=`// SPDX-License-Identifier: LicenseRef-Aitherium-Proprietary
// \xA9 2026 Aitherium, LLC. Original work.
// Original Aitherium WebGPU implementation.
//
// THE THREE OPS THE VAE DECODER NEEDS AND THE TRANSFORMER DOES NOT.
//
// In-browser image generation was written off as needing a foreign kernel family. It does
// not. \`:8798\` serves FLUX.2 Klein 4B, and the giveaway is in its own tensor names \u2014
// \`transformer_blocks.0.attn.to_q\` \u2014 MMDiT is a diffusion TRANSFORMER: attention + MLP over
// latent patches, which the existing kernels already do. Its text encoder is Qwen3-4B, the
// same architecture family as the Bonsai text models that already run in a visitor's browser.
//
// What genuinely has no equivalent is the VAE DECODER, and only three ops of it. From the
// shipped model's own vae/config.json (AutoencoderKLFlux2):
//
//     block_out_channels : [128, 256, 512, 512]
//     up_block_types     : 4 x UpDecoderBlock2D
//     layers_per_block   : 2
//     latent_channels    : 32
//     norm_num_groups    : 32
//     act_fn             : silu
//
// so the decode graph is: conv_in -> mid(resnet + attn) -> 4 x (2 resnets + 2x upsample)
// -> GroupNorm -> SiLU -> conv_out(3ch). Attention and SiLU already exist. These are the rest.
//
// LAYOUT: NCHW, f32, batch 1 \u2014 one image at a time is what a browser does, and NCHW keeps a
// channel's plane contiguous, which is what makes GroupNorm's reduction a simple range.
//
// PERFORMANCE NOTE, learned the expensive way on softmax_attn_batched: a kernel written as
// one-thread-per-output looks fine and silently becomes the bottleneck when the tensor grows.
// The last up block runs at full output resolution, so at 1024x1024x128 that is 134M outputs.
// conv2d here is one thread per OUTPUT ELEMENT with the reduction inside it \u2014 correct, and
// deliberately the simple version first, because the transformer kernels earned their
// optimisations only after a CPU differential proved them right. Optimise after it is correct
// and after a measurement says which part is slow, not before.

struct ConvP {
  in_c   : u32,
  out_c  : u32,
  h      : u32,   // input height
  w      : u32,   // input width
  k      : u32,   // square kernel size (1 or 3 here)
  pad    : u32,
  stride : u32,
  _p0    : u32,
};

@group(0) @binding(0) var<storage, read>       x       : array<f32>;  // [in_c*h*w]
@group(0) @binding(1) var<storage, read>       weight  : array<f32>;  // [out_c*in_c*k*k]
@group(0) @binding(2) var<storage, read>       bias    : array<f32>;  // [out_c]
@group(0) @binding(3) var<storage, read_write> y       : array<f32>;  // [out_c*oh*ow]
@group(0) @binding(4) var<uniform>             p       : ConvP;

fn out_h() -> u32 { return (p.h + 2u * p.pad - p.k) / p.stride + 1u; }
fn out_w() -> u32 { return (p.w + 2u * p.pad - p.k) / p.stride + 1u; }

/**
 * 2-D convolution, NCHW, one thread per output element.
 *
 * Zero padding is done by SKIPPING out-of-range taps rather than by materialising a padded
 * input. Materialising would allocate another full tensor per layer \u2014 at decoder resolutions
 * that is hundreds of megabytes of pure copy, on a device that is also holding a language
 * model.
 */
@compute @workgroup_size(64)
fn conv2d_main(@builtin(global_invocation_id) gid : vec3<u32>,
               @builtin(num_workgroups) nwg : vec3<u32>) {
  let oh = out_h();
  let ow = out_w();
  let total = p.out_c * oh * ow;
  let idx = gid.x + gid.y * nwg.x * 64u;
  if (idx >= total) { return; }

  let ox = idx % ow;
  let oy = (idx / ow) % oh;
  let oc = idx / (ow * oh);

  var acc : f32 = bias[oc];
  for (var ic : u32 = 0u; ic < p.in_c; ic = ic + 1u) {
    let x_plane = ic * p.h * p.w;
    let w_base = ((oc * p.in_c) + ic) * p.k * p.k;
    for (var ky : u32 = 0u; ky < p.k; ky = ky + 1u) {
      // Signed arithmetic: with pad=1 the first row's taps land at -1, and doing this in
      // u32 wraps to ~4 billion and reads far out of bounds. WebGPU's robust access would
      // return 0 there, which LOOKS like correct zero-padding and is not \u2014 it silently
      // drops the real taps too on the opposite edge.
      let iy = i32(oy * p.stride) + i32(ky) - i32(p.pad);
      if (iy < 0 || iy >= i32(p.h)) { continue; }
      for (var kx : u32 = 0u; kx < p.k; kx = kx + 1u) {
        let ix = i32(ox * p.stride) + i32(kx) - i32(p.pad);
        if (ix < 0 || ix >= i32(p.w)) { continue; }
        acc = acc + x[x_plane + u32(iy) * p.w + u32(ix)] * weight[w_base + ky * p.k + kx];
      }
    }
  }
  y[idx] = acc;
}

struct GroupNormP {
  c       : u32,
  h       : u32,
  w       : u32,
  groups  : u32,
  eps     : f32,
  _p0 : u32, _p1 : u32, _p2 : u32,
};

@group(0) @binding(0) var<storage, read>       gx      : array<f32>;
@group(0) @binding(1) var<storage, read>       gamma   : array<f32>;  // [c]
@group(0) @binding(2) var<storage, read>       beta    : array<f32>;  // [c]
@group(0) @binding(3) var<storage, read_write> gy      : array<f32>;
@group(0) @binding(4) var<uniform>             gp      : GroupNormP;

/**
 * GroupNorm \u2014 one WORKGROUP per group, cooperating over that group's whole slab.
 *
 * NOT one thread per group. A group at decoder sizes is (c/groups) x h x w elements \u2014 with
 * 128 channels, 32 groups and a 512x512 plane that is over a million values, and a single
 * thread walking it is the same one-lane mistake that made attention 8x slower than it had
 * to be. The mean and variance are a parallel reduction; the normalise pass is grid-strided.
 *
 * Two passes over the slab (mean, then variance) rather than the sum/sum-of-squares trick:
 * at f32 with a million-element reduction the one-pass form loses precision exactly where
 * the variance is small, which is where a VAE's activations live.
 */
var<workgroup> red_sum : array<f32, 256>;

@compute @workgroup_size(256)
fn groupnorm_main(@builtin(workgroup_id) wg : vec3<u32>,
                  @builtin(local_invocation_id) lid : vec3<u32>) {
  let g = wg.x;
  if (g >= gp.groups) { return; }        // uniform across the workgroup \u2014 safe with barriers

  let cpg = gp.c / gp.groups;            // channels per group
  let plane = gp.h * gp.w;
  let slab = cpg * plane;                // elements this group owns
  let base = g * slab;
  let tid = lid.x;

  // ---- mean ----
  var s : f32 = 0.0;
  var i : u32 = tid;
  loop {
    if (i >= slab) { break; }
    s = s + gx[base + i];
    i = i + 256u;
  }
  red_sum[tid] = s;
  workgroupBarrier();
  var stride : u32 = 128u;
  loop {
    if (stride == 0u) { break; }
    if (tid < stride) { red_sum[tid] = red_sum[tid] + red_sum[tid + stride]; }
    workgroupBarrier();
    stride = stride / 2u;
  }
  let mean = red_sum[0] / f32(slab);
  workgroupBarrier();

  // ---- variance ----
  var v : f32 = 0.0;
  i = tid;
  loop {
    if (i >= slab) { break; }
    let d = gx[base + i] - mean;
    v = v + d * d;
    i = i + 256u;
  }
  red_sum[tid] = v;
  workgroupBarrier();
  stride = 128u;
  loop {
    if (stride == 0u) { break; }
    if (tid < stride) { red_sum[tid] = red_sum[tid] + red_sum[tid + stride]; }
    workgroupBarrier();
    stride = stride / 2u;
  }
  let inv_std = 1.0 / sqrt(red_sum[0] / f32(slab) + gp.eps);
  workgroupBarrier();

  // ---- normalise + per-CHANNEL affine ----
  // gamma/beta are indexed by absolute channel, not by group: a group spans cpg channels and
  // each has its own scale. Using the group index here is an easy and completely silent
  // error \u2014 the image comes out plausible and wrong.
  i = tid;
  loop {
    if (i >= slab) { break; }
    let ch = g * cpg + (i / plane);
    gy[base + i] = (gx[base + i] - mean) * inv_std * gamma[ch] + beta[ch];
    i = i + 256u;
  }
}

struct UpP {
  c : u32,
  h : u32,
  w : u32,
  scale : u32,
};

@group(0) @binding(0) var<storage, read>       ux : array<f32>;
@group(0) @binding(1) var<storage, read_write> uy : array<f32>;
@group(0) @binding(2) var<uniform>             up : UpP;

/**
 * Nearest-neighbour upsample by an integer factor \u2014 what UpDecoderBlock2D does before its
 * convolution (diffusers' Upsample2D default is nearest, and the conv that follows is what
 * turns the blockiness into detail). Bilinear here would be a different model.
 */
@compute @workgroup_size(64)
fn upsample_nearest_main(@builtin(global_invocation_id) gid : vec3<u32>,
                         @builtin(num_workgroups) nwg : vec3<u32>) {
  let oh = up.h * up.scale;
  let ow = up.w * up.scale;
  let total = up.c * oh * ow;
  let idx = gid.x + gid.y * nwg.x * 64u;
  if (idx >= total) { return; }

  let ox = idx % ow;
  let oy = (idx / ow) % oh;
  let ch = idx / (ow * oh);

  let sx = ox / up.scale;
  let sy = oy / up.scale;
  uy[idx] = ux[ch * up.h * up.w + sy * up.w + sx];
}
`,Ci={causal_conv1d:Iu,deltanet:$u,deltanet_gate:Nu,deltanet_seq:Ru,elementwise:Gu,elementwise_inplace:Cu,image_ops:Du,kv_quant_4bit:qu,logit_topk:Mu,q1_0_dequant:Uu,q1_0_q8_0_matmul:Fu,q2_0_dequant:Ku,q2_0_q8_0_matmul:Wu,quantize_q8_0:ju,rmsnorm:Qu,rope_imrope:zu,sampling:Hu,softmax_attn:Yu,softmax_attn_batched:Xu,swiglu:Vu,vae_ops:Ju};function Di(){if(typeof navigator>"u")return!1;let e=navigator.userAgent??"";if(/Android|iPhone|iPad|iPod|Mobile|Silk|Kindle/i.test(e))return!0;let t=navigator.maxTouchPoints??0;return/Macintosh/i.test(e)&&t>1}var kn="https://huggingface.co/prism-ml",Zu="https://weights.aitherium.com",el=[{id:"bonsai-1.7b",label:"Bonsai 1.7B",params:"1.7B",sizeMb:236,url:`${kn}/Bonsai-1.7B-gguf/resolve/main/Bonsai-1.7B-Q1_0.gguf`,contextWindow:32768,blurb:"The lightest size \u2014 236 MB, runs on phones and older laptops.",arch:"qwen3"},{id:"bonsai-4b",label:"Bonsai 4B",params:"4B",sizeMb:545,url:`${kn}/Bonsai-4B-gguf/resolve/main/Bonsai-4B-Q1_0.gguf`,contextWindow:32768,blurb:"Balanced: smarter than 1.7B, quick to download and run.",arch:"qwen3"},{id:"bonsai-8b",label:"Bonsai 8B",params:"8B",sizeMb:1104,url:`${kn}/Bonsai-8B-gguf/resolve/main/Bonsai-8B-Q1_0.gguf`,contextWindow:65536,blurb:"Better reasoning, ~1 GB. Desktop GPU recommended.",arch:"qwen3"},{id:"bonsai-27b-text",label:"Bonsai 27B (Reasoning)",params:"27B",sizeMb:3627,url:`${kn}/Bonsai-27B-gguf/resolve/main/Bonsai-27B-Q1_0.gguf`,contextWindow:262144,blurb:"Full reasoning brain. 3.6 GB, needs a real desktop GPU.",arch:"qwen35"}];var Or="bonsai-4b";function qi(e){return el.find(t=>t.id===e)}function Mi(e){let t=qi(e)??qi(Or);return tl(t)}function tl(e){let t=e.url.split("/").pop();return t?`${Zu.replace(/\/+$/,"")}/${t}`:e.url}function Ui(){if(typeof navigator>"u")return Or;let e=navigator;if(e.connection?.saveData||e.connection?.effectiveType&&/2g/.test(e.connection.effectiveType))return"bonsai-1.7b";let n=e.deviceMemory??4;return Di()?n>=6?"bonsai-4b":"bonsai-1.7b":n>=8?"bonsai-8b":Or}Gi(self,{loadKernels:async()=>Ci,acquireDevice:async()=>{let e=navigator;if(!e.gpu)throw new Error("WebGPU unavailable (navigator.gpu missing)");let t=await e.gpu.requestAdapter({powerPreference:"high-performance"}),n=!1;if(t||(t=await e.gpu.requestAdapter({forceFallbackAdapter:!0}).catch(()=>null),n=t!==null),!t)throw new Error("no WebGPU adapter (even the software fallback refused)");let r=t.limits,o={};for(let a of["maxStorageBufferBindingSize","maxBufferSize","maxComputeWorkgroupStorageSize"]){let s=r[a];typeof s=="number"&&s>0&&(o[a]=s)}return await t.requestDevice({requiredLimits:o})},resolveModelUrl:e=>Mi(e==="auto"?Ui():e)});
