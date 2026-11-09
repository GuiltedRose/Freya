//! Active, bounded repair sessions. RPC supplies schedules, never shred payloads.
use super::*;
use super::acquisition::{atomic_file, checked, raw, reconstruct};
use solana_core::repair::serve_repair::{freya_repair_pong_checked, RepairProtocol, RepairRequestHeader, ServeRepair};
#[cfg(test)]
use solana_core::repair::serve_repair::freya_repair_pong;
use solana_gossip::contact_info::Protocol;
use solana_pubkey::Pubkey;
use solana_ledger::shred::Shred;
use std::{net::UdpSocket, process::{Command, Stdio}, str::FromStr};
const SCHEMA: &str = "freya-native-follow-v1";
fn response(packet:&[u8],from:SocketAddr,expected:SocketAddr,nonce:u32,slot:u64,
    version:u16,leader:&Pubkey,max:u32,index:Option<u64>) -> Result<(Shred,Vec<u8>), &'static str> {
    if from!=expected {return Err("unexpected_endpoint");}
    if packet.len()<4 || packet.len()>2048 {return Err("packet_framing");}
    let end=packet.len()-4;
    let trailer=packet[end..].try_into().map_err(|_|"packet_framing")?;
    if u32::from_le_bytes(trailer)!=nonce {return Err("nonce_mismatch");}
    let bytes=packet[..end].to_vec();
    let shred=checked(bytes.clone(),slot,version,leader,max).map_err(|_|"shred_validation_failed")?;
    if index.is_some_and(|i|i!=u64::from(shred.index())) {return Err("index_mismatch");}
    Ok((shred,bytes))
}
fn repair_address_allowed(address: &SocketAddr) -> bool {
    address.is_ipv4() && address.port()!=0 && SocketAddrSpace::Global.check(address)
        && !address.ip().is_unspecified() && !address.ip().is_multicast()
        && match address.ip() {
            std::net::IpAddr::V4(ip)=>!ip.is_link_local() && !ip.is_broadcast(),
            _=>false,
        }
}
fn repair_receive_deadline(start: Instant, overall: Instant, interval: u64, attempts: u64) -> Instant {
    // Use the former response + blind-backoff budget to receive from the same
    // authorized peer. No new wait is added after an unsuccessful attempt.
    let pacing=interval.saturating_mul(1u64<<attempts.min(4)).min(8000);
    (start+Duration::from_secs(2)+Duration::from_millis(pacing)).min(overall)
}
fn receive_error(error: io::Error, stopped: bool, before_deadline: bool) -> io::Result<bool> {
    match error.kind() {
        io::ErrorKind::Interrupted=>Ok(!stopped && before_deadline), // Retry only under the original loop deadline.
        io::ErrorKind::WouldBlock|io::ErrorKind::TimedOut=>Ok(true),
        _=>Err(error),
    }
}
fn repair_wait_result(accepted: bool, stopped: bool, deadline_reached: bool,
    pong_sent: bool) -> &'static str {
    if accepted {"validated_shred"}
    else if stopped {"stop_requested"}
    else if deadline_reached {"follow_deadline"}
    else if pong_sent {"ping_completed_without_shred"}
    else {"response_timeout"}
}
fn select_repair_peer<T: Copy + PartialEq>(peers: &[T], attempts: u64, pending: &mut Option<T>)
    -> io::Result<(T,bool)> {
    if let Some(peer)=pending.take().filter(|peer|peers.contains(peer)) {return Ok((peer,true));}
    if peers.is_empty() {return Err(invalid());}
    Ok((peers[attempts as usize%peers.len()],false))
}
fn increment(record: &mut Value, key: &str) {
    record[key]=json!(record[key].as_u64().unwrap_or(0).saturating_add(1));
}
fn finish_repair(state: &mut Value, result: &str) {
    if matches!(result,"response_timeout"|"ping_completed_without_shred") {increment(state,"repair_timeouts");}
    state["last_repair"]["outcome"]=json!(result);
    state["last_repair"]["finished_at"]=json!(now());
}
fn unexpected_datagram(record: &mut Value, from: SocketAddr, packet: &[u8], nonce: u32, at: f64) {
    increment(record,"unexpected_endpoint");
    // One untrusted metadata sample only; never decode, answer or admit this packet.
    if record["unexpected_sample"].is_null() {
        let nonce_matches=if (4..=2048).contains(&packet.len()) {
            packet[packet.len()-4..].try_into().ok().map(u32::from_le_bytes).map(|n|n==nonce)
        } else {None};
        record["unexpected_sample"]=json!({"source":from.to_string(),"received_at":at,
            "bytes":packet.len(),"nonce_matches":nonce_matches});
    }
}
fn remember_repair_send(state: &mut Value, at: f64) -> io::Result<()> {
    let record=&state["last_repair"];
    if record["send_confirmed"]!=true {return Err(invalid());}
    let mut entry=json!({"sent_at":at});
    for key in ["session_id","peer","endpoint","local_socket","nonce","slot","kind","request_bytes","handshake_followup"] {
        entry[key]=record[key].clone();
    }
    if state["repair_request_history"].is_null() {state["repair_request_history"]=json!([]);}
    let history=state["repair_request_history"].as_array_mut().ok_or_else(invalid)?;
    if history.len()>16 {return Err(invalid());}
    if history.len()==16 {history.remove(0);}
    history.push(entry);
    Ok(())
}
fn traffic_snapshot(state: &mut Value, phase: &str, counts: (u64,u64)) -> io::Result<()> {
    let traffic=&mut state["session_traffic"];
    let rx=counts.0.checked_sub(traffic["initial_rx"].as_u64().ok_or_else(invalid)?).ok_or_else(invalid)?;
    let tx=counts.1.checked_sub(traffic["initial_tx"].as_u64().ok_or_else(invalid)?).ok_or_else(invalid)?;
    let snapshots=traffic["interface_snapshots"].as_array_mut().ok_or_else(invalid)?;
    if snapshots.len()>=4 {return Err(invalid());}
    snapshots.push(json!({"phase":phase,"at":now(),"rx":rx,"tx":tx}));
    Ok(())
}
fn repair_error(state: &mut Value, operation: &str, error: io::Error) -> io::Error {
    let record=&mut state["last_repair"];
    record["failure_operation"]=json!(operation);
    record["socket_errno"]=json!(error.raw_os_error());
    record["error_kind"]=json!(match error.kind() {
        io::ErrorKind::PermissionDenied=>"permission_denied",
        io::ErrorKind::NotFound=>"not_found",
        io::ErrorKind::ConnectionRefused=>"connection_refused",
        io::ErrorKind::TimedOut=>"timed_out",
        io::ErrorKind::WouldBlock=>"would_block",
        _=>"io_error",
    });
    error
}
fn interrupted_repair(state: &mut Value, stopped: bool, deadline: bool, at: f64) {
    if !matches!(state["last_repair"]["outcome"].as_str(),Some("prepared"|"waiting")) {return;}
    let record=&mut state["last_repair"];
    let expired=record["lease_expires"].as_f64().is_some_and(|until|at>=until);
    let reason=match record["failure_operation"].as_str() {
        Some("lease_check")=>if expired {"network_lease_expired"} else {"network_authorization_invalid"},
        Some("request_send"|"pong_send"|"socket_receive")=>"socket_error",
        Some("counter_read")=>"counter_read_failed",
        _=>if stopped {"stop_requested"} else if deadline {"follow_deadline"} else {"request_interrupted"},
    };
    record["outcome"]=json!(reason);
    record["stop_observed"]=json!(stopped);
    record["follow_deadline_reached"]=json!(deadline);
    record["lease_expired_at_finish"]=json!(expired);
    record["finished_at"]=json!(at);
}
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
fn structured_source(value: &Value) -> io::Result<&Value> {
    let object=value.as_object().ok_or_else(invalid)?;
    if object.len()!=2 {return Err(invalid());}
    for key in ["provider_id","endpoint_id"] {
        let digest=object.get(key).and_then(Value::as_str).ok_or_else(invalid)?;
        if digest.len()!=64 || !digest.bytes().all(|b|b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) {
            return Err(invalid());
        }
    }
    Ok(value)
}
fn checked_source<'a>(window: &'a Value, state: &Value) -> io::Result<&'a Value> {
    let source=structured_source(&window["source_identity"])?;
    let prior=&state["rpc_source_identity"];
    // Missing identity is a first use. Malformed or legacy identities require
    // explicit operator review; never silently discard an existing source pin.
    if !prior.is_null() && structured_source(prior)?!=source {return Err(invalid());}
    Ok(source)
}
fn initialize_window_state(window: &Value, state: &mut Value, version: u16) -> io::Result<()> {
    let first=window["start"].as_u64().ok_or_else(invalid)?;
    let end=window["end"].as_u64().ok_or_else(invalid)?;
    let source=checked_source(window,state)?.clone();
    if end<first || end-first>3 || bounded(window,"shred_version",1,65535)?!=u64::from(version) {
        return Err(invalid());
    }
    if state["window_start"].as_u64()==Some(first) {
        let cursor=state["next_slot"].as_u64().ok_or_else(invalid)?;
        if cursor<first || cursor>end.saturating_add(1) {return Err(invalid());}
    }
    state["rpc_source_identity"]=source;
    if state["window_start"].as_u64()!=Some(first) {
        state["window_start"]=json!(first);state["next_slot"]=json!(first);state["attempts"]=json!(0);
    }
    Ok(())
}
fn resume_slot(state: &Value, window: Option<&Value>) -> io::Result<u64> {
    if !state["next_slot"].is_null() {
        return state["next_slot"].as_u64().ok_or_else(invalid);
    }
    if let Some(window)=window {
        let first=window["start"].as_u64().ok_or_else(invalid)?;
        let end=window["end"].as_u64().ok_or_else(invalid)?;
        if window["schema"]!="freya-native-window-v1" || window["verification"]!="reviewed_rpc_claim"
            || window["genesis"]!="5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
            || end<first || end-first>3 {return Err(invalid());}
        return Ok(first); // Resume the retained window; never infer completion from file counts.
    }
    Ok(0)
}
fn resume_window(dir: &Path, state: &Value) -> io::Result<Option<Value>> {
    if dir.join("window.json").exists() {return read_json(&dir.join("window.json")).map(Some);}
    if state["next_slot"].is_u64() {return Ok(None);}
    let mut earliest: Option<Value>=None;
    for entry in fs::read_dir(dir)? {
        let entry=entry?;
        let Some(slot)=entry.file_name().to_str().and_then(|name|name.parse::<u64>().ok()) else {continue;};
        if !fs::symlink_metadata(entry.path())?.is_dir() {return Err(invalid());}
        let window=read_json(&entry.path().join("schedule.json"))?;
        let start=resume_slot(state,Some(&window))?;
        if slot<start || slot>window["end"].as_u64().ok_or_else(invalid)? {return Err(invalid());}
        checked_source(&window,state)?;
        if let Some(prior)=earliest.as_ref() {
            if prior["source_identity"]!=window["source_identity"] {return Err(invalid());}
        }
        if earliest.as_ref().is_none_or(|prior|start<prior["start"].as_u64().unwrap_or(0)) {earliest=Some(window);}
    }
    Ok(earliest)
}
fn interruption(deadline_reached: bool, stopped: bool, lease_valid: bool) -> Option<&'static str> {
    if !lease_valid {Some("network_lease_invalid")}
    else if stopped {Some("stop_requested")}
    else if deadline_reached {Some("planning_deadline")}
    else {None}
}
fn planning_result(dir: &Path, run_id: &str, begun: Instant, category: &str,
    status: Option<std::process::ExitStatus>) -> io::Result<()> {
    use std::os::unix::process::ExitStatusExt;
    let at=SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs_f64();
    let mut record=json!({
        "schema":"freya-native-planning-supervisor-v1", "run_id":run_id,
        "at":at, "elapsed_seconds":begun.elapsed().as_secs_f64(),
        "category":category, "exit_code":status.and_then(|s|s.code()),
        "signal":status.and_then(|s|s.signal()),
        "window_validated":category=="planner_success"
    });
    if let Ok(progress)=read_json(&dir.join("planning-progress.json")) {
        if progress["run_id"].as_str()==Some(run_id) {
            if let Some(stage)=progress["stage"].as_str().filter(|stage|matches!(*stage,
                "rpc_validation"|"finalized_head"|"history_floor"|"leader_schedule"|
                "finalized_manifest"|"history_recheck"|"cluster_nodes"|"cluster_metadata"|"manifest_validation"|"window_publication")) {
                record["stage"]=json!(stage);
            }
            if let Some(method)=progress["rpc_method"].as_str().filter(|method|matches!(*method,
                "getGenesisHash"|"getSlot"|"getFirstAvailableBlock"|"getSlotLeaders"|"getBlocks"|"getClusterNodes")) {
                record["rpc_method"]=json!(method);
            }
            if let Some(start)=progress["stage_started_at"].as_f64().filter(|start|start.is_finite() && *start<=at && *start>=0.0) {
                record["stage_elapsed_seconds"]=json!(at-start);
            }
            let timing=&progress["rpc_timing"];
            if let Some(phase)=timing["phase"].as_str().filter(|phase|matches!(*phase,
                "provider_reservation"|"provider_backoff"|"provider_reservation_finished"|
                "http_request"|"http_in_progress"|"http_finished"|"response_validation")) {
                record["rpc_phase"]=json!(phase);
            }
            for key in ["next_wait_seconds","reservation_elapsed_ms","request_headers_ms",
                        "response_body_ms","bytes_received"] {
                if let Some(value)=timing[key].as_f64().filter(|v|v.is_finite() && *v>=0.0 && *v<=9_007_199_254_740_992.0) {
                    record[key]=json!(value);
                }
            }
            if let Some(value)=progress["status"].as_str().filter(|value|matches!(*value,
                "started"|"running"|"stage_completed"|"failed"|"caught_up"|"completed")) {
                record["planner_status"]=json!(value);
            }
        }
    }
    save(&dir.join("planning-supervisor.json"), &record)
}
fn exited_planner(status: std::process::ExitStatus, stopped: bool, lease_valid: bool) -> &'static str {
    use std::os::unix::process::ExitStatusExt;
    if status.success() {"planner_success"}
    else if status.code()==Some(75) {"planner_caught_up"}
    else if status.code()==Some(76) {"provider_cooldown_exceeds_lease"}
    else if matches!(status.signal(),Some(15)|Some(2)) {
        if !lease_valid {"network_lease_invalid"}
        else if stopped {"stop_requested"}
        else {"planner_external_signal"}
    } else {"planner_process_failure"}
}
fn supervise_child(child: &mut std::process::Child, deadline: Option<Instant>, stop: &AtomicBool,
    lease_valid: impl Fn() -> bool) -> (&'static str, Option<std::process::ExitStatus>) {
    loop {
        // A completed child wins over a concurrent deadline observation.
        match child.try_wait() {
            Ok(Some(status))=>return (exited_planner(status,stop.load(Ordering::Relaxed),lease_valid()),Some(status)),
            Ok(None)=>(),
            Err(_)=>{
                let _=child.kill();return ("planner_process_failure",child.wait().ok());
            }
        }
        if let Some(reason)=interruption(deadline.is_some_and(|until|Instant::now()>=until),stop.load(Ordering::Relaxed),lease_valid()) {
            let _=child.kill();return (reason,child.wait().ok());
        }
        thread::sleep(Duration::from_millis(100));
    }
}
fn plan(dir: &Path, start: Option<u64>, stop: &AtomicBool) -> io::Result<Value> {
    loop {
        let begun=Instant::now();
        let nonce=SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_nanos();
        let run_id=format!("{:032x}",nonce ^ u128::from(std::process::id()));
        planning_result(dir,&run_id,begun,"planning_started",None)?;
        // A fresh supervisor record invalidates prior progress/errors even if Python cannot start.
        let authorization=lease();
        if let Some(reason)=interruption(false,stop.load(Ordering::Relaxed),authorization.is_ok()) {
            planning_result(dir,&run_id,begun,reason,None)?;
            return Err(io::Error::other(reason));
        }
        let expires=authorization?["expires"].as_f64().ok_or_else(invalid)?;
        let launched=(|| {
            let mut command=Command::new(env::var_os("FREYA_NODE_PYTHON").ok_or_else(invalid)?);
            command.arg(env::var_os("FREYA_NODE_PLANNER").ok_or_else(invalid)?)
                .arg("--config").arg(env::var_os("FREYA_NODE_EXTERNAL_CONFIG").ok_or_else(invalid)?)
                .arg("--output").arg(dir.join("window.json"))
                .arg("--run-id").arg(&run_id)
                .env("FREYA_PLANNING_LEASE_EXPIRES",expires.to_string());
            if let Some(start)=start {command.arg("--start").arg(start.to_string());}
            command.stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).spawn()
        })();
        let mut child=match launched {
            Ok(child)=>child,
            Err(error)=>{planning_result(dir,&run_id,begun,"planner_process_failure",None)?;return Err(error);}
        };
        let (observed,status)=supervise_child(&mut child,None,stop,||lease().is_ok());
        let at=SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs_f64();
        let reason=if matches!(observed,"network_lease_invalid"|"stop_requested"|"planner_external_signal") && at>=expires {
            "network_lease_expired"
        } else {observed};
        if !matches!(reason,"planner_success"|"planner_caught_up") {
            planning_result(dir,&run_id,begun,reason,status)?;
            return Err(io::Error::other(reason));
        }
        let status=status.ok_or_else(invalid)?;
        if status.code()==Some(75) {
            planning_result(dir,&run_id,begun,"planner_caught_up",Some(status))?;
            let until=Instant::now()+Duration::from_secs(2);
            while Instant::now()<until {
                if let Some(reason)=interruption(false,stop.load(Ordering::Relaxed),lease().is_ok()) {
                    planning_result(dir,&run_id,begun,reason,Some(status))?;
                    return Err(io::Error::other(reason));
                }
                thread::sleep(Duration::from_millis(100));
            }
            continue;
        }
        let validated=(|| {
            let window=read_json(&dir.join("window.json"))?;
            if window["planning_run_id"].as_str()!=Some(run_id.as_str())
                || window["schema"]!="freya-native-window-v1" || window["verification"]!="reviewed_rpc_claim"
                || window["genesis"]!="5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
                || start.is_some_and(|s|window["start"].as_u64()!=Some(s)) {return Err(invalid());}
            Ok(window)
        })();
        planning_result(dir,&run_id,begun,if validated.is_ok(){"planner_success"}else{"invalid_rpc_metadata"},Some(status))?;
        return validated;
    }
}

pub fn run(config_path: &Path, dir: &Path) -> io::Result<()> {
    let config = read_json(config_path)?;
    let continuous = config["continuous"].as_bool().unwrap_or(false);
    let seconds = if continuous {86400} else {bounded(&config,"duration_seconds",1,50)?};
    let traffic_cap = bounded(&config,"max_namespace_bytes",1,if continuous {10_000_000}else{1_000_000})?;
    let disk_cap = bounded(&config,"max_disk_bytes",1_048_576,1_073_741_824)?;
    let max = bounded(&config,"max_shreds_per_slot",1,4096)? as u32;
    let max_attempts = bounded(&config,"max_attempts_per_window",1,4096)?;
    let interval = bounded(&config,"request_interval_ms",250,10_000)?;
    let configured_bootstraps = if config["bootstrap_peers"].is_null() {vec![]}
        else {config["bootstrap_peers"].as_array().ok_or_else(invalid)?.clone()};
    let mut bootstraps = configured_bootstraps.iter()
        .map(|v|v.as_str().ok_or_else(invalid)?.parse::<SocketAddr>().map_err(|_|invalid())).collect::<io::Result<Vec<_>>>()?;
    if bootstraps.len()>4 || bootstraps.iter().any(|a|
        !a.is_ipv4() || a.ip().is_loopback() || a.ip().is_unspecified() || a.port()==0) { return Err(invalid()); }
    let _lock = private_dir(dir)?;
    NATIVE_DISK_LIMIT.set((dir.to_path_buf(),disk_cap)).map_err(|_|invalid())?;
    let mut state = if dir.join("checkpoint.json").exists() {read_json(&dir.join("checkpoint.json"))?}
        else {json!({"format":SCHEMA,"schema_version":1,"next_slot":null,"attempts":0,"window_start":null,
            "validated_shreds":0,"duplicates":0,"repair_requests":0,"repair_responses":0,"repair_timeouts":0})};
    if state["format"]!=SCHEMA || state["schema_version"]!=1 { return Err(invalid()); }
    for field in ["attempts","validated_shreds","duplicates","repair_requests","repair_responses","repair_timeouts"] {
        if !state[field].is_u64() {return Err(invalid());}
    }
    for field in ["next_slot","window_start"] {
        if !state[field].is_null() && !state[field].is_u64() {return Err(invalid());}
    }
    if !state["window_start"].is_null() && (state["next_slot"].is_null() || !dir.join("window.json").is_file()) {return Err(invalid());}
    if !state["repair_request_history"].is_null()
        && state["repair_request_history"].as_array().is_none_or(|rows|rows.len()>16) {return Err(invalid());}
    state["state"] = json!("starting"); state["native_ready"] = json!(false);
    state["verification"] = json!("RPC schedule plus leader-signed shreds; no replay or consensus");
    state["external_rpc_dependency"] = json!(["genesis","finalized manifest","leader schedule","shred version","execution receipts"]);
    state["source_mode"] = json!("native_repair_with_external_metadata");
    state["coding_recovery"] = json!("available for persisted parity; data-index repair requests data only");
    state["receipt_availability"] = json!("external reconciliation required");
    state["continuous"] = json!(continuous);
    state["peer_connected"] = json!(0); state["active_repair_peer"] = Value::Null;
    state["queue_in_flight"] = json!(0); state["last_error_category"] = Value::Null;
    publish(dir,&state)?;
    let initial_authorization=lease();
    if initial_authorization.is_err() {
        state["state"]=json!("blocked");state["last_error_category"]=json!("network_authorization");
        publish(dir,&state)?;return Err(invalid());
    }
    state["network_lease_expires"]=initial_authorization?["expires"].clone();
    let initial = counters()?;
    let stop = Arc::new(AtomicBool::new(false));
    signal_hook::flag::register(signal_hook::consts::SIGINT,stop.clone())?;
    signal_hook::flag::register(signal_hook::consts::SIGTERM,stop.clone())?;
    let key_path = dir.join("identity.json");
    if !state["node_identity"].is_null() && !key_path.exists() {return Err(invalid());}
    if !key_path.exists() { save(&key_path,&json!(Keypair::new().to_bytes().to_vec()))?; }
    let meta = fs::symlink_metadata(&key_path)?;
    if !meta.is_file() || meta.uid()!=effective_uid()? || meta.permissions().mode()&0o077!=0 { return Err(invalid()); }
    let key = read_keypair_file(&key_path).map_err(|_|invalid())?;
    let request_key = read_keypair_file(&key_path).map_err(|_|invalid())?;
    if state["node_identity"].as_str().is_some_and(|id|id!=request_key.pubkey().to_string()) {return Err(invalid());}
    state["node_identity"]=json!(request_key.pubkey().to_string());publish(dir,&state)?;
    let repair_session_id=format!("{}-{}",std::process::id(),
        SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_nanos());
    state["repair_session_id"]=json!(repair_session_id);
    state["session_traffic"]=json!({"session_id":repair_session_id,
        "interface_units":"namespace_eth0_counter_bytes",
        "payload_units":"udp_application_bytes",
        "initial_rx":initial.0,"initial_tx":initial.1,"interface_snapshots":[],
        "repair_request_payload_tx":0,"repair_pong_payload_tx":0,
        "repair_socket_rx_copied_bytes":0,"repair_socket_rx_datagrams":0,
        "repair_socket_rx_buffer_full":0});
    let mut failure_stage="rpc_planning";
    let outcome = (|| -> io::Result<()> {
        let retained=resume_window(dir,&state)?;
        let start=resume_slot(&state,retained.as_ref())?;
        if let Some(window)=retained.as_ref() {
            // Keep the original source pin even when only slot schedules survived.
            state["rpc_source_identity"]=checked_source(window,&state)?.clone();
        }
        let mut window = match retained {
            Some(window) if state["next_slot"].is_u64()=>window,
            _=>plan(dir,Some(start),&stop)?, // Revalidate orphaned planning metadata before reuse.
        };
        if window["schema"]!="freya-native-window-v1" || window["genesis"]!="5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d" { return Err(invalid()); }
        failure_stage="bootstrap_metadata";
        if bootstraps.is_empty() {
            if window["bootstrap_nodes"].as_array().is_none_or(|rows|rows.is_empty()) {
                window=plan(dir,window["start"].as_u64(),&stop)?;
            }
            if window["bootstrap_provenance"]!="reviewed_rpc_claim" {return Err(invalid());}
            let peers=window["bootstrap_nodes"].as_array().ok_or_else(invalid)?;
            if peers.is_empty() || peers.len()>4 {return Err(io::Error::other("no compatible public gossip bootstrap candidates"));}
            for peer in peers {
                Pubkey::from_str(peer["identity"].as_str().ok_or_else(invalid)?).map_err(|_|invalid())?;
                let socket=peer["gossip"].as_str().ok_or_else(invalid)?.parse::<SocketAddr>().map_err(|_|invalid())?;
                if !socket.is_ipv4() || socket.ip().is_loopback() || socket.ip().is_unspecified() || socket.port()==0 {return Err(invalid());}
                bootstraps.push(socket);
            }
            state["bootstrap_source"]=json!("reviewed_rpc_claim");
            state["bootstrap_nodes"]=window["bootstrap_nodes"].clone();
        } else {state["bootstrap_source"]=json!("explicit_configuration");}
        failure_stage="rpc_source_identity";
        checked_source(&window,&state)?;
        state["bootstrap_peers"]=json!(bootstraps.iter().map(ToString::to_string).collect::<Vec<_>>());
        publish(dir,&state)?;
        traffic_snapshot(&mut state,"planning_finished",counters()?)?;
        // RPC planning is bounded by the independent network lease, not the repair timer.
        let deadline=Instant::now()+Duration::from_secs(seconds);
        failure_stage="native_socket_initialization";
        let version = bounded(&window,"shred_version",1,65535)? as u16;
        initialize_window_state(&window,&mut state,version)?;
        state["state"]=json!("discovering");publish(dir,&state)?;
        let socket = UdpSocket::bind("0.0.0.0:0")?;
        socket.set_read_timeout(Some(Duration::from_millis(100)))?;
        failure_stage="gossip_initialization";
        traffic_snapshot(&mut state,"gossip_start",counters()?)?;
        let (service,_,cluster) = make_node(key,&bootstraps,stop.clone(),None,version,true,SocketAddrSpace::Global);
        let mut first_eligible_recorded=false;
        let mut handshake_retry=None;
        let session = (|| -> io::Result<()> {
            while Instant::now()<deadline && !stop.load(Ordering::Relaxed) {
                failure_stage="native_network_or_resource_policy";
                lease()?;
                let usage = counters()?;
                let rx=usage.0.checked_sub(initial.0).ok_or_else(invalid)?;
                let tx=usage.1.checked_sub(initial.1).ok_or_else(invalid)?;
                state["namespace_incoming_bytes"]=json!(rx);state["namespace_outgoing_bytes"]=json!(tx);
                let disk=disk_bytes(dir)?;state["disk_bytes"]=json!(disk);
                state["ram_bytes"]=json!(fs::read_to_string("/proc/self/status").ok().and_then(|s|
                    s.lines().find_map(|l|l.strip_prefix("VmRSS:").and_then(|v|v.split_whitespace().next()?.parse::<u64>().ok()).map(|kb|kb*1024))));
                if rx.saturating_add(tx)>=traffic_cap || disk.saturating_add(65536)>disk_cap { return Err(io::Error::other("resource bound")); }
                failure_stage="native_window_validation";
                initialize_window_state(&window,&mut state,version)?;
                let first=window["start"].as_u64().ok_or_else(invalid)?;
                let end=window["end"].as_u64().ok_or_else(invalid)?;
                let cursor=state["next_slot"].as_u64().ok_or_else(invalid)?;
                state["observed_network_slot"]=window["observed_finalized_head"].clone();
                state["network_progress_provenance"]=json!("finalized RPC claim");
                let age=(now() as f64-window["at"].as_f64().ok_or_else(invalid)?).max(0.0);
                state["head_age_seconds"]=json!(age);state["lag_is_current"]=json!(age<=30.0);
                state["estimated_lag_slots"]=json!(window["observed_finalized_head"].as_u64().map(|head|head.saturating_sub(cursor)));
                if cursor<first || cursor>end+1 { return Err(invalid()); }
                if cursor>end {
                    state["state"]=json!("following"); publish(dir,&state)?;
                    failure_stage="rpc_planning";
                    window=plan(dir,Some(cursor),&stop)?;
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
                failure_stage="native_reconstruction";
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
                failure_stage="native_peer_selection";
                let peers: Vec<_>=discovered.into_iter().filter(|(p,t)|p.shred_version()==version
                    && now().saturating_mul(1000).saturating_sub(*t)<=30_000 && p.pubkey()!=&cluster.id())
                    .filter_map(|(p,_)|p.serve_repair(Protocol::UDP).filter(repair_address_allowed).map(|a|(*p.pubkey(),a))).take(64).collect();
                state["peer_connected"]=json!(peers.len());
                if peers.is_empty(){state["state"]=json!("discovering");publish(dir,&state)?;thread::sleep(Duration::from_millis(100));continue;}
                if !first_eligible_recorded {
                    traffic_snapshot(&mut state,"first_eligible_repair_peers",counters()?)?;
                    first_eligible_recorded=true;
                }
                let ((peer,address),handshake_followup)=select_repair_peer(&peers,attempts,&mut handshake_retry)?;
                let nonce=rand::random::<u32>();
                let header=RepairRequestHeader::new(request_key.pubkey(),peer,now().saturating_mul(1000),nonce);
                let index=report["missing_indices"].as_array().and_then(|a|a.first()).and_then(Value::as_u64);
                let highest=report["terminal_index"].is_null();
                let request=if highest {RepairProtocol::HighestWindowIndex{header,slot:cursor,shred_index:0}}
                    else{RepairProtocol::WindowIndex{header,slot:cursor,shred_index:index.ok_or_else(invalid)?}};
                let payload=ServeRepair::repair_proto_to_bytes(&request,&request_key).map_err(|_|invalid())?;
                state["attempts"]=json!(attempts+1);state["repair_requests"]=json!(state["repair_requests"].as_u64().unwrap_or(0)+1);
                state["active_repair_peer"]=json!(peer.to_string());state["state"]=json!("recovering");state["queue_in_flight"]=json!(1);
                failure_stage="native_repair_io";
                state["last_repair"]=json!({"session_id":repair_session_id,"started_at":now(),
                    "slot":cursor,"peer":peer.to_string(),"endpoint":address.to_string(),
                    "local_socket":socket.local_addr()?.to_string(),"nonce":nonce,
                    "kind":if highest {"highest_window_index"} else {"window_index"},
                    "index":if highest {Some(0)} else {index},"request_bytes":payload.len(),
                    "handshake_followup":handshake_followup,
                    "lease_expires":state["network_lease_expires"],
                    "send_confirmed":false,"outcome":"prepared","expected_datagrams":0,
                    "unexpected_endpoint":0,"oversize_datagrams":0,"rejected_datagrams":0,
                    "authenticated_pings":0,"pongs_sent":0});
                publish(dir,&state)?;
                if stop.load(Ordering::Relaxed) || Instant::now()>=deadline {break;}
                let authorization=lease().map_err(|e|repair_error(&mut state,"lease_check",e))?;
                state["last_repair"]["lease_expires"]=authorization["expires"].clone();
                if socket.send_to(&payload,address).map_err(|e|repair_error(&mut state,"request_send",e))?!=payload.len() {return Err(invalid());}
                state["session_traffic"]["repair_request_payload_tx"]=json!(
                    state["session_traffic"]["repair_request_payload_tx"].as_u64().ok_or_else(invalid)?
                        .saturating_add(payload.len() as u64));
                state["last_repair"]["send_confirmed"]=json!(true);
                remember_repair_send(&mut state,
                    SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs_f64())?;
                state["last_repair"]["outcome"]=json!("waiting");
                publish(dir,&state)?;
                let receive_started=Instant::now();
                let timeout=repair_receive_deadline(receive_started,deadline,interval,attempts);
                let receive_at=SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs_f64();
                state["last_repair"]["receive_started_at"]=json!(receive_at);
                state["last_repair"]["receive_deadline_at"]=json!(receive_at+timeout.duration_since(receive_started).as_secs_f64());
                let mut packet=[0u8;2049];let mut accepted=false;let mut pong_sent=false;
                while Instant::now()<timeout && !stop.load(Ordering::Relaxed) {
                    lease().map_err(|e|repair_error(&mut state,"lease_check",e))?;
                    let usage=counters().map_err(|e|repair_error(&mut state,"counter_read",e))?;
                    if usage.0.checked_sub(initial.0).ok_or_else(invalid)?.saturating_add(usage.1.checked_sub(initial.1).ok_or_else(invalid)?)>=traffic_cap {return Err(invalid());}
                    let (length,from)=match socket.recv_from(&mut packet) {
                        Ok(value)=>value,
                        Err(error)=>match receive_error(error,stop.load(Ordering::Relaxed),Instant::now()<timeout) {
                            Ok(true)=>continue,
                            Ok(false)=>break,
                            Err(error)=>return Err(repair_error(&mut state,"socket_receive",error)),
                        }
                    };
                    increment(&mut state["session_traffic"],"repair_socket_rx_datagrams");
                    state["session_traffic"]["repair_socket_rx_copied_bytes"]=json!(
                        state["session_traffic"]["repair_socket_rx_copied_bytes"].as_u64().ok_or_else(invalid)?
                            .saturating_add(length as u64));
                    if length==packet.len() {increment(&mut state["session_traffic"],"repair_socket_rx_buffer_full");}
                    if stop.load(Ordering::Relaxed) || Instant::now()>=timeout {break;}
                    if from!=address {
                        unexpected_datagram(&mut state["last_repair"],from,&packet[..length],nonce,
                            SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs_f64());
                        continue;
                    }
                    increment(&mut state["last_repair"],"expected_datagrams");
                    if length>2048 {increment(&mut state["last_repair"],"oversize_datagrams");continue;}
                    state["last_repair"]["last_expected_received_at"]=json!(
                        SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs_f64());
                    if !pong_sent && length==132 && packet[..4]==[0;4] {
                        match freya_repair_pong_checked(&packet[..length],&request_key,&peer) {
                        Ok(pong)=>{
                            increment(&mut state["last_repair"],"authenticated_pings");
                            if stop.load(Ordering::Relaxed) || Instant::now()>=deadline {break;}
                            lease().map_err(|e|repair_error(&mut state,"lease_check",e))?;
                            if socket.send_to(&pong,address).map_err(|e|repair_error(&mut state,"pong_send",e))?!=pong.len() {return Err(invalid());}
                            state["session_traffic"]["repair_pong_payload_tx"]=json!(
                                state["session_traffic"]["repair_pong_payload_tx"].as_u64().ok_or_else(invalid)?
                                    .saturating_add(pong.len() as u64));
                            increment(&mut state["last_repair"],"pongs_sent");
                            state["last_repair"]["pong_sent_at"]=json!(
                                SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs_f64());
                            pong_sent=true;publish(dir,&state)?;continue;
                        }
                        Err(reason)=>{increment(&mut state["last_repair"],"rejected_datagrams");
                            state["last_repair"]["last_rejection"]=json!(reason);continue;}
                        }
                    }
                    let (shred,bytes)=match response(&packet[..length],from,address,nonce,cursor,version,&leader,max,
                        if highest{None}else{index}) {
                        Ok(value)=>value,
                        Err(reason)=>{increment(&mut state["last_repair"],"rejected_datagrams");
                            state["last_repair"]["last_rejection"]=json!(reason);continue;}
                    };
                    let path=base.join(format!("{}.shred",shred.index()));
                    if path.exists(){
                        if raw(&path)?!=bytes {save(&base.join("conflict.json"),&json!({"conflict":true}))?;atomic_file(&base.join("conflicting-shred.bin"),&bytes)?;return Err(invalid());}
                        state["duplicates"]=json!(state["duplicates"].as_u64().unwrap_or(0)+1);
                    }else{atomic_file(&path,&bytes)?;state["validated_shreds"]=json!(state["validated_shreds"].as_u64().unwrap_or(0)+1);}
                    state["repair_responses"]=json!(state["repair_responses"].as_u64().unwrap_or(0)+1);accepted=true;break;
                }
                let result=repair_wait_result(accepted,stop.load(Ordering::Relaxed),Instant::now()>=deadline,pong_sent);
                // One follow-up uses an existing paced attempt, never an extra send.
                if !handshake_followup && result=="ping_completed_without_shred" {
                    handshake_retry=Some((peer,address));
                }
                finish_repair(&mut state,result);
                state["queue_in_flight"]=json!(0);state["updated_at"]=json!(now());publish(dir,&state)?;
                let until=if accepted {(Instant::now()+Duration::from_millis(interval)).min(deadline)}
                    else {timeout};
                while Instant::now()<until && !stop.load(Ordering::Relaxed){lease()?;thread::sleep(Duration::from_millis(100));}
            }
            Ok(())
        })();
        // Capture the real cancellation flag before setting it for gossip teardown.
        if state["last_repair"]["session_id"].as_str()==Some(repair_session_id.as_str()) {
            interrupted_repair(&mut state,stop.load(Ordering::Relaxed),Instant::now()>=deadline,
                SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs_f64());
            if session.is_err() {state["last_repair"]["failure_stage"]=json!(failure_stage);}
        }
        stop.store(true,Ordering::Relaxed);
        state["queue_in_flight"]=json!(0);state["state"]=json!("stopping");
        // Persist failure evidence before a potentially slow gossip join.
        let diagnostic=publish(dir,&state);
        let joined=service.join();diagnostic?;session?;joined.map_err(|_|invalid())
    })();
    if state["last_repair"]["session_id"].as_str()==Some(repair_session_id.as_str())
        && matches!(state["last_repair"]["outcome"].as_str(),Some("prepared"|"waiting")) {
        interrupted_repair(&mut state,stop.load(Ordering::Relaxed),false,
            SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs_f64());
        state["last_repair"]["failure_stage"]=json!(failure_stage);
        state["last_repair"]["finished_at"]=json!(now());
    }
    // Optional final measurement must not mask the actual session result when the
    // controller has already removed the interface. Earlier snapshots remain.
    if counters().and_then(|counts|traffic_snapshot(&mut state,"finished",counts)).is_err() {
        state["session_traffic"]["final_snapshot_unavailable"]=json!(true);
    }
    state["state"]=json!(if outcome.is_ok(){"stopped"}else{"blocked"});state["queue_in_flight"]=json!(0);
    if outcome.is_err(){state["last_error_category"]=json!(failure_stage);}
    state["updated_at"]=json!(now());publish(dir,&state)?;outcome
}

#[cfg(test)]
mod tests {
    use super::*;
    fn source_fixture() -> Value {
        // ProviderBudget.identity returns two lowercase HMAC-SHA256 hex digests.
        json!({"provider_id":"a".repeat(64),"endpoint_id":"b".repeat(64)})
    }
    #[test] fn startup_resumes_checkpoint_then_retained_window_otherwise_zero() {
        let window=json!({"schema":"freya-native-window-v1","verification":"reviewed_rpc_claim",
            "genesis":"5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d","start":7,"end":10});
        assert_eq!(resume_slot(&json!({"next_slot":9}),Some(&window)).unwrap(),9);
        assert_eq!(resume_slot(&json!({"next_slot":null}),Some(&window)).unwrap(),7);
        assert_eq!(resume_slot(&json!({"next_slot":null}),None).unwrap(),0);
        assert!(resume_slot(&json!({"next_slot":"corrupt"}),None).is_err());
        assert!(resume_slot(&json!({"next_slot":null}),Some(&json!({"start":7}))).is_err());
    }
    #[test] fn retained_slot_schedules_resume_earliest_window_without_skipping_gaps() {
        let dir=tempfile::tempdir().unwrap();
        let state=json!({"next_slot":null});
        assert!(resume_window(dir.path(),&state).unwrap().is_none());
        for slot in [11,7] {
            let base=dir.path().join(slot.to_string());fs::create_dir(&base).unwrap();
            fs::write(base.join("schedule.json"),serde_json::to_vec(&json!({
                "schema":"freya-native-window-v1","verification":"reviewed_rpc_claim",
                "genesis":"5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d",
                "source_identity":source_fixture(),"start":slot,"end":slot+3})).unwrap()).unwrap();
        }
        let window=resume_window(dir.path(),&state).unwrap().unwrap();
        assert_eq!(resume_slot(&state,Some(&window)).unwrap(),7);
        fs::create_dir(dir.path().join("3")).unwrap(); // Orphaned bytes must not imply verified progress.
        assert!(resume_window(dir.path(),&state).is_err());
    }
    #[test] fn structured_planner_identity_is_persisted_and_reusable() {
        let window=json!({"schema":"freya-native-window-v1","source_identity":source_fixture()});
        let mut state=json!({"next_slot":null});
        state["rpc_source_identity"]=checked_source(&window,&state).unwrap().clone();
        let restored:Value=serde_json::from_slice(&serde_json::to_vec(&state).unwrap()).unwrap();
        assert_eq!(checked_source(&window,&restored).unwrap(),&source_fixture());
        assert_eq!(restored["next_slot"],Value::Null);
    }
    #[test] fn structured_source_switches_are_rejected() {
        let window=json!({"source_identity":source_fixture()});
        for key in ["provider_id","endpoint_id"] {
            let mut prior=source_fixture();prior[key]=json!("c".repeat(64));
            assert!(checked_source(&window,&json!({"rpc_source_identity":prior})).is_err());
        }
    }
    #[test] fn malformed_and_legacy_source_pins_fail_closed() {
        let good=json!({"source_identity":source_fixture()});
        for bad in [json!("legacy-pin"),json!({}),json!({"provider_id":"a".repeat(64)}),
            json!({"provider_id":"https://private.invalid/SECRET","endpoint_id":"b".repeat(64)}),
            json!({"provider_id":7,"endpoint_id":"b".repeat(64)})] {
            assert!(checked_source(&json!({"source_identity":bad.clone()}),&json!({})).is_err());
            let state=json!({"rpc_source_identity":bad});
            let before=state.clone();
            let error=checked_source(&good,&state).unwrap_err();
            assert!(!error.to_string().contains("SECRET"));assert_eq!(state,before);
        }
        assert!(checked_source(&json!({}),&json!({})).is_err());
    }
    #[test] fn validated_window_initializes_native_checkpoint_and_preserves_cursor() {
        let window=json!({"source_identity":source_fixture(),"start":7,"end":10,"shred_version":50093});
        let mut state=json!({"window_start":null,"next_slot":null,"attempts":0,"native_ready":false});
        initialize_window_state(&window,&mut state,50093).unwrap();
        assert_eq!(state["window_start"],7);assert_eq!(state["next_slot"],7);
        assert_eq!(state["rpc_source_identity"],source_fixture());
        state["next_slot"]=json!(9);state["attempts"]=json!(2);
        initialize_window_state(&window,&mut state,50093).unwrap();
        assert_eq!(state["next_slot"],9);assert_eq!(state["attempts"],2);
        assert_eq!(state["native_ready"],false);
        let before=state.clone();
        assert!(initialize_window_state(&window,&mut state,7).is_err());assert_eq!(state,before);
    }
    #[test] fn signalled_planner_is_not_automatically_a_process_failure() {
        use std::os::unix::process::ExitStatusExt;
        let status=std::process::ExitStatus::from_raw(15);
        assert_eq!(exited_planner(status,false,false),"network_lease_invalid");
        assert_eq!(exited_planner(status,true,true),"stop_requested");
        assert_eq!(exited_planner(status,false,true),"planner_external_signal");
        assert_eq!(exited_planner(std::process::ExitStatus::from_raw(256),true,false),"planner_process_failure");
    }
    fn child_result(script: &str, duration: Duration, stopped: bool, valid: bool) -> (&'static str, Option<std::process::ExitStatus>) {
        let mut child=Command::new("sh").arg("-c").arg(script).spawn().unwrap();
        supervise_child(&mut child,Some(Instant::now()+duration),&AtomicBool::new(stopped),||valid)
    }
    #[test] fn planner_deadline_is_distinct() {
        assert_eq!(child_result("exec sleep 5",Duration::ZERO,false,true).0,"planning_deadline");
    }
    #[test] fn planning_without_local_deadline_still_obeys_lease() {
        let mut child=Command::new("sh").arg("-c").arg("exec sleep 5").spawn().unwrap();
        assert_eq!(supervise_child(&mut child,None,&AtomicBool::new(false),||false).0,
            "network_lease_invalid");
    }
    #[test] fn planning_without_local_deadline_can_complete() {
        let mut child=Command::new("sh").arg("-c").arg("exit 0").spawn().unwrap();
        assert_eq!(supervise_child(&mut child,None,&AtomicBool::new(false),||true).0,"planner_success");
    }
    #[test] fn planner_stop_is_distinct() {
        assert_eq!(child_result("exec sleep 5",Duration::from_secs(2),true,true).0,"stop_requested");
    }
    #[test] fn planner_lease_is_distinct() {
        assert_eq!(child_result("exec sleep 5",Duration::from_secs(2),false,false).0,"network_lease_invalid");
    }
    #[test] fn planner_exit_is_distinct() {
        let (reason,status)=child_result("exit 9",Duration::from_secs(5),false,true);
        assert_eq!(reason,"planner_process_failure");assert_eq!(status.unwrap().code(),Some(9));
    }
    #[test] fn planner_success_is_distinct() {
        assert_eq!(child_result("exit 0",Duration::from_secs(5),false,true).0,"planner_success");
    }
    #[test] fn cooldown_lease_expiry_keeps_final_supervisor_evidence() {
        let dir=tempfile::tempdir().unwrap();
        fs::write(dir.path().join("planning-progress.json"),serde_json::to_vec(&json!({
            "run_id":"cooldown","stage":"cluster_nodes","rpc_method":"getClusterNodes",
            "status":"running","rpc_timing":{"phase":"provider_backoff","next_wait_seconds":29.85}
        })).unwrap()).unwrap();
        let (reason,status)=child_result("exec sleep 5",Duration::from_secs(2),false,false);
        assert_eq!(reason,"network_lease_invalid");
        planning_result(dir.path(),"cooldown",Instant::now(),"network_lease_expired",status).unwrap();
        let record=read_json(&dir.path().join("planning-supervisor.json")).unwrap();
        assert_eq!(record["category"],"network_lease_expired");
        assert_eq!(record["rpc_phase"],"provider_backoff");
        assert_eq!(record["next_wait_seconds"],29.85);
        assert_eq!(record["window_validated"],false);
        assert_eq!(child_result("exit 76",Duration::from_secs(2),false,true).0,
                   "provider_cooldown_exceeds_lease");
    }
    #[test] fn supervisor_replaces_stale_run_without_copying_secrets() {
        let dir=tempfile::tempdir().unwrap();
        fs::write(dir.path().join("planning-error.json"),b"SECRET https://private.invalid").unwrap();
        fs::write(dir.path().join("planning-progress.json"),serde_json::to_vec(&json!({
            "run_id":"current","stage":"finalized_head","rpc_method":"getSlot",
            "status":"running","stage_started_at":now(),"secret":"SECRET https://private.invalid"
        })).unwrap()).unwrap();
        planning_result(dir.path(),"current",Instant::now(),"planning_deadline",None).unwrap();
        let record=read_json(&dir.path().join("planning-supervisor.json")).unwrap();
        assert_eq!(record["run_id"],"current");assert_eq!(record["category"],"planning_deadline");
        assert!(!record.to_string().contains("SECRET"));assert_eq!(record["window_validated"],false);
        assert_eq!(record["stage"],"finalized_head");assert_eq!(record["rpc_method"],"getSlot");
    }
    #[test] fn repair_candidates_obey_global_socket_policy() {
        assert!(repair_address_allowed(&"1.1.1.1:8001".parse().unwrap()));
        for value in ["127.0.0.1:8001","10.0.0.1:8001","192.168.1.1:8001",
                      "169.254.1.1:8001","0.0.0.0:8001","224.0.0.1:8001",
                      "255.255.255.255:8001","1.1.1.1:0","[::1]:8001"] {
            assert!(!repair_address_allowed(&value.parse().unwrap()),"{value}");
        }
    }
    #[test] fn handshake_followup_requires_same_eligible_identity_and_endpoint() {
        let mut pending=Some((1,8008));
        assert_eq!(select_repair_peer(&[(1,8008),(2,8009)],1,&mut pending).unwrap(),((1,8008),true));
        assert!(pending.is_none());
        assert_eq!(select_repair_peer(&[(1,8008),(2,8009)],1,&mut pending).unwrap(),((2,8009),false));
        for stale in [(1,9000),(3,8008)] {
            pending=Some(stale);
            assert_eq!(select_repair_peer(&[(1,8008),(2,8009)],1,&mut pending).unwrap(),((2,8009),false));
        }
        assert!(select_repair_peer::<(u8,u16)>(&[],0,&mut None).is_err());
    }
    #[test] fn traffic_snapshots_use_counter_deltas_and_bound_history() {
        let mut state=json!({"repair_requests":17,"session_traffic":{
            "initial_rx":100,"initial_tx":200,"interface_snapshots":[]}});
        traffic_snapshot(&mut state,"planning_finished",(150,275)).unwrap();
        assert_eq!(state["session_traffic"]["interface_snapshots"][0]["rx"],50);
        assert_eq!(state["session_traffic"]["interface_snapshots"][0]["tx"],75);
        assert!(traffic_snapshot(&mut state,"reset",(99,275)).is_err());
        for _ in 0..3 {traffic_snapshot(&mut state,"phase",(150,275)).unwrap();}
        assert!(traffic_snapshot(&mut state,"overflow",(150,275)).is_err());
        assert_eq!(state["repair_requests"],17);
    }
    #[test] fn receive_window_uses_existing_backoff_without_increasing_attempt_budget() {
        let start=Instant::now();let overall=start+Duration::from_secs(40);
        assert_eq!(repair_receive_deadline(start,overall,500,0),start+Duration::from_millis(2500));
        let deadline=repair_receive_deadline(start,overall,500,4);
        assert_eq!(deadline,start+Duration::from_secs(10));
        // The observed valid ping arrived 3.705 seconds after kernel send:
        // outside the old two-second wait, inside the already allocated budget.
        assert!(start+Duration::from_millis(3705)<deadline);
        assert_eq!(repair_receive_deadline(start,start+Duration::from_secs(1),500,4),start+Duration::from_secs(1));
        assert_eq!(repair_receive_deadline(start,overall,10000,128),start+Duration::from_secs(10));
    }
    #[test] fn interrupted_receive_during_stop_is_graceful_not_socket_error() {
        let mut state=json!({"repair_timeouts":7,"last_repair":{"outcome":"waiting"}});
        let retry=receive_error(io::Error::from_raw_os_error(4),true,true).unwrap();
        assert!(!retry);
        finish_repair(&mut state,repair_wait_result(false,true,false,false));
        assert_eq!(state["last_repair"]["outcome"],"stop_requested");
        assert!(state["last_repair"]["failure_operation"].is_null());
        assert_eq!(state["repair_timeouts"],7);
    }
    #[test] fn interrupted_receive_without_stop_uses_original_deadline() {
        let deadline=Instant::now(); // Already expired: EINTR cannot create a fresh window.
        assert!(receive_error(io::Error::from_raw_os_error(4),false,true).unwrap());
        assert!(!receive_error(io::Error::from_raw_os_error(4),false,Instant::now()<deadline).unwrap());
        assert_eq!(repair_wait_result(false,false,Instant::now()>=deadline,false),"follow_deadline");
        for kind in [io::ErrorKind::WouldBlock,io::ErrorKind::TimedOut] {
            assert!(receive_error(io::Error::from(kind),false,true).unwrap());
        }
    }
    #[test] fn real_receive_failures_remain_errors_even_during_stop() {
        for stopped in [false,true] {
            assert_eq!(receive_error(io::Error::from_raw_os_error(113),stopped,true).unwrap_err().raw_os_error(),Some(113));
            assert_eq!(receive_error(io::Error::from(io::ErrorKind::PermissionDenied),stopped,true).unwrap_err().kind(),io::ErrorKind::PermissionDenied);
        }
    }
    #[test] fn repair_wait_separates_stop_deadline_handshake_and_timeout() {
        assert_eq!(repair_wait_result(false,true,false,false),"stop_requested");
        assert_eq!(repair_wait_result(false,false,true,false),"follow_deadline");
        assert_eq!(repair_wait_result(false,false,false,true),"ping_completed_without_shred");
        assert_eq!(repair_wait_result(false,false,false,false),"response_timeout");
        assert_eq!(repair_wait_result(true,true,true,true),"validated_shred");
    }
    #[test] fn interrupted_wait_preserves_timeout_and_shred_counters() {
        let mut state=json!({"repair_requests":8,"repair_timeouts":2,"validated_shreds":3,"last_repair":{}});
        for result in ["stop_requested","follow_deadline","validated_shred"] {
            finish_repair(&mut state,result);
            assert_eq!(state["last_repair"]["outcome"],result);
            assert_eq!(state["repair_timeouts"],2);
            assert_eq!(state["repair_requests"],8);assert_eq!(state["validated_shreds"],3);
        }
        finish_repair(&mut state,"response_timeout");
        assert_eq!(state["repair_timeouts"],3);
        finish_repair(&mut state,"ping_completed_without_shred");
        assert_eq!(state["repair_timeouts"],4);
    }
    #[test] fn confirmed_send_history_retains_prior_peer_and_stays_bounded() {
        let mut state=json!({"repair_requests":20,"last_repair":{"send_confirmed":false}});
        assert!(remember_repair_send(&mut state,1.0).is_err());
        assert!(state["repair_request_history"].is_null());
        for nonce in 0..20 {
            state["last_repair"]=json!({"send_confirmed":true,"session_id":"session",
                "peer":"public identity","endpoint":"185.8.106.229:8009",
                "local_socket":"0.0.0.0:49458","nonce":nonce,"slot":42,
                "kind":"highest_window_index","request_bytes":160});
            remember_repair_send(&mut state,nonce as f64).unwrap();
        }
        assert_eq!(state["repair_request_history"].as_array().unwrap().len(),16);
        assert_eq!(state["repair_request_history"][0]["nonce"],4);
        assert_eq!(state["repair_request_history"][15]["nonce"],19);
        assert_eq!(state["repair_request_history"][15]["endpoint"],"185.8.106.229:8009");
        assert_eq!(state["repair_requests"],20);
        assert!(state["validated_shreds"].is_null());
    }
    #[test] fn unexpected_datagram_records_first_source_without_admitting_data() {
        let mut record=json!({"unexpected_endpoint":0,"expected_datagrams":0});
        unexpected_datagram(&mut record,"1.1.1.1:8008".parse().unwrap(),&17u32.to_le_bytes(),17,100.5);
        assert_eq!(record["unexpected_sample"]["source"],"1.1.1.1:8008");
        assert_eq!(record["unexpected_sample"]["nonce_matches"],true);
        unexpected_datagram(&mut record,"8.8.8.8:8008".parse().unwrap(),&[],17,101.0);
        assert_eq!(record["unexpected_endpoint"],2);
        assert_eq!(record["unexpected_sample"]["source"],"1.1.1.1:8008");
        assert_eq!(record["expected_datagrams"],0);
        assert!(record["validated_shreds"].is_null());
        let mut short=json!({});
        unexpected_datagram(&mut short,"1.1.1.1:8008".parse().unwrap(),&[],17,102.0);
        assert!(short["unexpected_sample"]["nonce_matches"].is_null());
    }
    #[test] fn repair_interruption_retains_lease_and_socket_failure_evidence() {
        let mut state=json!({"repair_timeouts":1,"last_repair":{"outcome":"waiting","lease_expires":155.0}});
        let error=io::Error::new(io::ErrorKind::NotFound,"SECRET lease path");
        let _=repair_error(&mut state,"lease_check",error);
        interrupted_repair(&mut state,false,false,155.1);
        assert_eq!(state["last_repair"]["outcome"],"network_lease_expired");
        assert_eq!(state["last_repair"]["stop_observed"],false);
        assert_eq!(state["repair_timeouts"],1);
        assert!(!state.to_string().contains("SECRET"));
        // Teardown setting stop=true must not replace the captured error.
        interrupted_repair(&mut state,true,false,156.0);
        assert_eq!(state["last_repair"]["outcome"],"network_lease_expired");
        state["last_repair"]=json!({"outcome":"waiting","lease_expires":155.0});
        let _=repair_error(&mut state,"socket_receive",io::Error::from_raw_os_error(113));
        interrupted_repair(&mut state,false,false,154.0);
        assert_eq!(state["last_repair"]["outcome"],"socket_error");
        assert_eq!(state["last_repair"]["socket_errno"],113);
    }
    #[test] fn repair_interruption_distinguishes_revocation_stop_and_deadline() {
        for (operation,stopped,deadline,reason) in [
            ("lease_check",false,false,"network_authorization_invalid"),
            ("counter_read",false,false,"counter_read_failed"),
            ("",true,false,"stop_requested"),("",false,true,"follow_deadline")] {
            let mut state=json!({"last_repair":{"outcome":"waiting","lease_expires":155.0}});
            if !operation.is_empty() {let _=repair_error(&mut state,operation,invalid());}
            interrupted_repair(&mut state,stopped,deadline,154.0);
            assert_eq!(state["last_repair"]["outcome"],reason);
        }
        let mut accepted=json!({"last_repair":{"outcome":"validated_shred"}});
        interrupted_repair(&mut accepted,true,true,156.0);
        assert_eq!(accepted["last_repair"]["outcome"],"validated_shred");
    }
    #[test] fn response_binds_socket_nonce_slot_version_leader_and_index() {
        let (key,shreds)=super::super::acquisition::reconstruction_tests::fixture();
        let data=shreds.iter().find(|s|s.is_data()).unwrap();
        let mut packet=data.payload().as_ref().to_vec();packet.extend_from_slice(&17u32.to_le_bytes());
        let address="127.0.0.1:12345".parse().unwrap();let other="127.0.0.1:12346".parse().unwrap();
        let accept=|from,nonce,slot,version,leader:&Pubkey,index|response(&packet,from,address,nonce,slot,version,leader,4096,index).is_ok();
        assert!(accept(address,17,42,7,&key.pubkey(),Some(0)));
        assert_eq!(response(&packet,address,address,18,42,7,&key.pubkey(),4096,None).err(),Some("nonce_mismatch"));
        assert_eq!(response(&packet[..3],address,address,17,42,7,&key.pubkey(),4096,None).err(),Some("packet_framing"));
        assert_eq!(response(&packet,address,address,17,43,7,&key.pubkey(),4096,None).err(),Some("shred_validation_failed"));

        assert!(!accept(other,17,42,7,&key.pubkey(),Some(0)));
        assert!(!accept(address,18,42,7,&key.pubkey(),Some(0)));
        assert!(!accept(address,17,43,7,&key.pubkey(),Some(0)));
        assert!(!accept(address,17,42,8,&key.pubkey(),Some(0)));
        assert!(!accept(address,17,42,7,&Keypair::new().pubkey(),Some(0)));
        assert!(!accept(address,17,42,7,&key.pubkey(),Some(1)));
    }
    #[test] fn ping_requires_expected_peer_and_returns_authenticated_pong() {
        use solana_keypair::signable::Signable;
        #[derive(wincode::SchemaWrite)] enum Reply {Ping(solana_gossip::ping_pong::Ping<32>)}
        let peer=Keypair::new();let node=Keypair::new();
        let data=wincode::serialize(&Reply::Ping(solana_gossip::ping_pong::Ping::new([7;32],&peer))).unwrap();
        let bytes=freya_repair_pong(&data,&node,&peer.pubkey()).unwrap();
        let RepairProtocol::Pong(pong)=wincode::deserialize(&bytes).unwrap() else {panic!("expected pong")};
        assert!(pong.verify());assert_eq!(pong.pubkey(),node.pubkey());
        assert!(freya_repair_pong(&data,&node,&node.pubkey()).is_none());
        assert!(freya_repair_pong(b"corrupt",&node,&peer.pubkey()).is_none());
        assert_eq!(freya_repair_pong_checked(&data,&node,&node.pubkey()).err(),Some("ping_identity_mismatch"));
        let mut forged=data.clone();forged[68]^=1;
        assert_eq!(freya_repair_pong_checked(&forged,&node,&peer.pubkey()).err(),Some("ping_signature_invalid"));
        assert_eq!(freya_repair_pong_checked(&data[..131],&node,&peer.pubkey()).err(),Some("ping_framing"));

    }
}
