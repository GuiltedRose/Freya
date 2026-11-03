//! Active, bounded repair sessions. RPC supplies schedules, never shred payloads.
use super::*;
use super::acquisition::{atomic_file, checked, raw, reconstruct};
use solana_core::repair::serve_repair::{freya_repair_pong, RepairProtocol, RepairRequestHeader, ServeRepair};
use solana_gossip::contact_info::Protocol;
use solana_pubkey::Pubkey;
use std::{net::UdpSocket, process::{Command, Stdio}, str::FromStr};
const SCHEMA: &str = "freya-native-follow-v1";
fn save(path: &Path, value: &Value) -> io::Result<()> {
    atomic_file(path,&serde_json::to_vec(value).map_err(|_|invalid())?)
}
pub(super) fn disk_bytes(dir: &Path) -> io::Result<u64> {
    let mut total = 0u64;
    for row in fs::read_dir(dir)? {
        let path = row?.path(); let meta = fs::symlink_metadata(&path)?;
        if meta.is_symlink() { return Err(invalid()); }
        total = total.checked_add(if meta.is_dir(){disk_bytes(&path)?}else{meta.len()}).ok_or_else(invalid)?;
    }
    Ok(total)
}
fn plan(dir: &Path, start: Option<u64>, deadline: Instant, stop: &AtomicBool) -> io::Result<Value> {
    lease()?;
    let mut command = Command::new(env::var_os("FREYA_NODE_PYTHON").ok_or_else(invalid)?);
    command.arg(env::var_os("FREYA_NODE_PLANNER").ok_or_else(invalid)?)
        .arg("--config").arg(env::var_os("FREYA_NODE_EXTERNAL_CONFIG").ok_or_else(invalid)?)
        .arg("--output").arg(dir.join("window.json"));
    if let Some(start) = start { command.arg("--start").arg(start.to_string()); }
    let mut child = command.stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).spawn()?;
    loop {
        if Instant::now()>=deadline || stop.load(Ordering::Relaxed) || lease().is_err() {
            let _ = child.kill(); let _ = child.wait(); return Err(io::Error::other("planning interrupted"));
        }
        if let Some(status) = child.try_wait()? {
            if !status.success() { return Err(io::Error::other("reviewed RPC plan unavailable")); }
            break;
        }
        thread::sleep(Duration::from_millis(100));
    }
    let window = read_json(&dir.join("window.json"))?;
    if window["schema"]!="freya-native-window-v1" || window["verification"]!="reviewed_rpc_claim"
        || window["genesis"]!="5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
        || start.is_some_and(|s|window["start"].as_u64()!=Some(s)) { return Err(invalid()); }
    Ok(window)
}
pub fn run(config_path: &Path, dir: &Path) -> io::Result<()> {
    let config = read_json(config_path)?;
    let seconds = bounded(&config,"duration_seconds",1,50)?;
    let traffic_cap = bounded(&config,"max_namespace_bytes",1,1_000_000)?;
    let disk_cap = bounded(&config,"max_disk_bytes",1_048_576,1_073_741_824)?;
    let max = bounded(&config,"max_shreds_per_slot",1,4096)? as u32;
    let max_attempts = bounded(&config,"max_attempts_per_window",1,4096)?;
    let interval = bounded(&config,"request_interval_ms",250,10_000)?;
    let bootstraps = config["bootstrap_peers"].as_array().ok_or_else(invalid)?.iter()
        .map(|v|v.as_str().ok_or_else(invalid)?.parse::<SocketAddr>().map_err(|_|invalid())).collect::<io::Result<Vec<_>>>()?;
    if bootstraps.is_empty() || bootstraps.len()>4 || bootstraps.iter().any(|a|
        !a.is_ipv4() || a.ip().is_loopback() || a.ip().is_unspecified() || a.port()==0) { return Err(invalid()); }
    let _lock = private_dir(dir)?;
    NATIVE_DISK_LIMIT.set((dir.to_path_buf(),disk_cap)).map_err(|_|invalid())?;
    let mut state = if dir.join("checkpoint.json").exists() {read_json(&dir.join("checkpoint.json"))?}
        else {json!({"format":SCHEMA,"schema_version":1,"next_slot":null,"attempts":0,"window_start":null,
            "validated_shreds":0,"duplicates":0,"repair_requests":0,"repair_responses":0,"repair_timeouts":0})};
    if state["format"]!=SCHEMA || state["schema_version"]!=1 { return Err(invalid()); }
    state["state"] = json!("starting"); state["native_ready"] = json!(false);
    state["verification"] = json!("RPC schedule plus leader-signed shreds; no replay or consensus");
    state["external_rpc_dependency"] = json!(["genesis","finalized manifest","leader schedule","shred version","execution receipts"]);
    state["source_mode"] = json!("native_repair_with_external_metadata");
    state["coding_recovery"] = Value::Null; state["receipt_availability"] = json!("external reconciliation required");
    state["peer_connected"] = json!(0); state["active_repair_peer"] = Value::Null;
    state["queue_in_flight"] = json!(0); state["last_error_category"] = Value::Null;
    publish(dir,&state)?;
    lease()?;
    let initial = counters()?;
    let deadline = Instant::now()+Duration::from_secs(seconds);
    let stop = Arc::new(AtomicBool::new(false));
    signal_hook::flag::register(signal_hook::consts::SIGINT,stop.clone())?;
    signal_hook::flag::register(signal_hook::consts::SIGTERM,stop.clone())?;
    let key_path = dir.join("identity.json");
    if !key_path.exists() { save(&key_path,&json!(Keypair::new().to_bytes().to_vec()))?; }
    let meta = fs::symlink_metadata(&key_path)?;
    if !meta.is_file() || meta.uid()!=fs::metadata("/proc/self")?.uid() || meta.permissions().mode()&0o077!=0 { return Err(invalid()); }
    let key = read_keypair_file(&key_path).map_err(|_|invalid())?;
    let request_key = read_keypair_file(&key_path).map_err(|_|invalid())?;
    let outcome = (|| -> io::Result<()> {
        let mut window = if dir.join("window.json").exists() && state["next_slot"].is_u64() {
            read_json(&dir.join("window.json"))?
        } else { plan(dir,state["next_slot"].as_u64(),deadline,&stop)? };
        if window["schema"]!="freya-native-window-v1" || window["genesis"]!="5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d" { return Err(invalid()); }
        let version = bounded(&window,"shred_version",1,65535)? as u16;
        let socket = UdpSocket::bind("0.0.0.0:0")?;
        socket.set_read_timeout(Some(Duration::from_millis(100)))?;
        let (service,_,cluster) = make_node(key,&bootstraps,stop.clone(),None,version,true,SocketAddrSpace::Global);
        let session = (|| -> io::Result<()> {
            while Instant::now()<deadline && !stop.load(Ordering::Relaxed) {
                lease()?;
                let usage = counters()?;
                let rx=usage.0.checked_sub(initial.0).ok_or_else(invalid)?;
                let tx=usage.1.checked_sub(initial.1).ok_or_else(invalid)?;
                state["namespace_incoming_bytes"]=json!(rx);state["namespace_outgoing_bytes"]=json!(tx);
                if rx.saturating_add(tx)>=traffic_cap || disk_bytes(dir)?.saturating_add(65536)>disk_cap { return Err(io::Error::other("resource bound")); }
                let first=window["start"].as_u64().ok_or_else(invalid)?;
                let source=window["source_identity"].as_str().ok_or_else(invalid)?;
                if state["rpc_source_identity"].as_str().is_some_and(|old|old!=source) { return Err(invalid()); }
                state["rpc_source_identity"]=json!(source);
                let end=window["end"].as_u64().ok_or_else(invalid)?;
                if end<first || end-first>3 || bounded(&window,"shred_version",1,65535)?!=u64::from(version) { return Err(invalid()); }
                if state["window_start"].as_u64()!=Some(first) {
                    state["window_start"]=json!(first); state["next_slot"]=json!(first);state["attempts"]=json!(0);
                }
                let cursor=state["next_slot"].as_u64().ok_or_else(invalid)?;
                if cursor<first || cursor>end+1 { return Err(invalid()); }
                if cursor>end {
                    state["state"]=json!("following"); publish(dir,&state)?;
                    window=plan(dir,Some(cursor),deadline,&stop)?;
                    continue;
                }
                let rows=window["slots"].as_array().ok_or_else(invalid)?;
                if rows.len()>4 { return Err(invalid()); }
                let row=rows.iter().find(|row|row["slot"].as_u64()==Some(cursor));
                if row.is_none() {
                    if !window["rpc_reported_skipped_slots"].as_array().ok_or_else(invalid)?.contains(&json!(cursor)) { return Err(invalid()); }
                    // Persist RPC skip provenance, never interpret a repair timeout as a skip.
                    save(&dir.join(format!("{cursor}.rpc-skip.json")),&window)?;
                    state["next_slot"]=json!(cursor+1);publish(dir,&state)?;continue;
                }
                let leader=Pubkey::from_str(row.unwrap()["leader"].as_str().ok_or_else(invalid)?).map_err(|_|invalid())?;
                let base=dir.join(cursor.to_string());
                if !base.exists(){fs::create_dir(&base)?;fs::set_permissions(&base,fs::Permissions::from_mode(0o700))?;}
                if !fs::symlink_metadata(&base)?.is_dir() || base.join("conflict.json").exists() {return Err(invalid());}
                save(&base.join("schedule.json"),&window)?;
                let report=reconstruct(&base,cursor,version,&leader,max)?;
                if report["reconstructed"]==true {
                    state["last_reconstructed_slot"]=json!(cursor);state["next_slot"]=json!(cursor+1);
                    state["state"]=json!("catch_up");publish(dir,&state)?;continue;
                }
                state["incomplete_slot"]=json!(cursor);state["missing_indices"]=report["missing_indices"].clone();
                state["terminal_index"]=report["terminal_index"].clone();
                let attempts=state["attempts"].as_u64().ok_or_else(invalid)?;
                if attempts>=max_attempts {return Err(io::Error::other("repair retry bound; unresolved gap preserved"));}
                let discovered=cluster.all_peers();
                if discovered.len()>512 {return Err(io::Error::other("peer capacity"));}
                let peers: Vec<_>=discovered.into_iter().filter(|(p,t)|p.shred_version()==version
                    && now().saturating_mul(1000).saturating_sub(*t)<=30_000 && p.pubkey()!=&cluster.id())
                    .filter_map(|(p,_)|p.serve_repair(Protocol::UDP).filter(|a|a.is_ipv4()).map(|a|(*p.pubkey(),a))).take(64).collect();
                state["peer_connected"]=json!(peers.len());
                if peers.is_empty(){state["state"]=json!("discovering");publish(dir,&state)?;thread::sleep(Duration::from_millis(100));continue;}
                let (peer,address)=peers[attempts as usize%peers.len()];
                let nonce=rand::random::<u32>();
                let header=RepairRequestHeader::new(request_key.pubkey(),peer,now().saturating_mul(1000),nonce);
                let index=report["missing_indices"].as_array().and_then(|a|a.first()).and_then(Value::as_u64);
                let highest=report["terminal_index"].is_null();
                let request=if highest {RepairProtocol::HighestWindowIndex{header,slot:cursor,shred_index:0}}
                    else{RepairProtocol::WindowIndex{header,slot:cursor,shred_index:index.ok_or_else(invalid)?}};
                let payload=ServeRepair::repair_proto_to_bytes(&request,&request_key).map_err(|_|invalid())?;
                state["attempts"]=json!(attempts+1);state["repair_requests"]=json!(state["repair_requests"].as_u64().unwrap_or(0)+1);
                state["active_repair_peer"]=json!(peer.to_string());state["state"]=json!("recovering");state["queue_in_flight"]=json!(1);
                publish(dir,&state)?;socket.send_to(&payload,address)?;
                let timeout=(Instant::now()+Duration::from_secs(2)).min(deadline);
                let mut packet=[0u8;2049];let mut accepted=false;let mut pong_sent=false;
                while Instant::now()<timeout && !stop.load(Ordering::Relaxed) {
                    lease()?;
                    let usage=counters()?;
                    if usage.0.checked_sub(initial.0).ok_or_else(invalid)?.saturating_add(usage.1.checked_sub(initial.1).ok_or_else(invalid)?)>=traffic_cap {return Err(invalid());}
                    let (length,from)=match socket.recv_from(&mut packet){Ok(v)=>v,Err(e) if matches!(e.kind(),io::ErrorKind::WouldBlock|io::ErrorKind::TimedOut)=>continue,Err(e)=>return Err(e)};
                    if from!=address || length>2048 {continue;}
                    if !pong_sent {
                        if let Some(pong)=freya_repair_pong(&packet[..length],&request_key,&peer){socket.send_to(&pong,address)?;pong_sent=true;continue;}
                    }
                    if length<4 || u32::from_le_bytes(packet[length-4..length].try_into().map_err(|_|invalid())?)!=nonce {continue;}
                    let bytes=packet[..length-4].to_vec();
                    let shred=match checked(bytes.clone(),cursor,version,&leader,max){Ok(s)=>s,Err(_)=>continue};
                    if !highest && Some(u64::from(shred.index()))!=index {continue;}
                    let path=base.join(format!("{}.shred",shred.index()));
                    if path.exists(){
                        if raw(&path)?!=bytes {save(&base.join("conflict.json"),&json!({"conflict":true}))?;atomic_file(&base.join("conflicting-shred.bin"),&bytes)?;return Err(invalid());}
                        state["duplicates"]=json!(state["duplicates"].as_u64().unwrap_or(0)+1);
                    }else{atomic_file(&path,&bytes)?;state["validated_shreds"]=json!(state["validated_shreds"].as_u64().unwrap_or(0)+1);}
                    state["repair_responses"]=json!(state["repair_responses"].as_u64().unwrap_or(0)+1);accepted=true;break;
                }
                if !accepted {state["repair_timeouts"]=json!(state["repair_timeouts"].as_u64().unwrap_or(0)+1);}
                state["queue_in_flight"]=json!(0);state["updated_at"]=json!(now());publish(dir,&state)?;
                let wait=if accepted{interval}else{interval.saturating_mul(1u64<<attempts.min(4)).min(8000)};
                let until=(Instant::now()+Duration::from_millis(wait)).min(deadline);
                while Instant::now()<until && !stop.load(Ordering::Relaxed){lease()?;thread::sleep(Duration::from_millis(100));}
            }
            Ok(())
        })();
        stop.store(true,Ordering::Relaxed);let joined=service.join();session?;joined.map_err(|_|invalid())
    })();
    state["state"]=json!(if outcome.is_ok(){"stopped"}else{"blocked"});state["queue_in_flight"]=json!(0);
    if outcome.is_err(){state["last_error_category"]=json!("repair_planning_or_network_policy");}
    state["updated_at"]=json!(now());publish(dir,&state)?;outcome
}
