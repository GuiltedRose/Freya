//! Experimental bounded transport only; kernel namespace/quota remain authoritative.
use pyo3::prelude::*;
use pyo3::exceptions::{PyConnectionError, PyRuntimeError, PyValueError};
use futures_util::{SinkExt, StreamExt};
use std::{sync::Arc, time::{Duration, Instant}};
use tokio::sync::{Mutex, Semaphore};
use tokio_tungstenite::{connect_async_with_config, MaybeTlsStream, WebSocketStream};
use tokio_tungstenite::tungstenite::{Message, protocol::WebSocketConfig};
use base64::Engine;

fn bad(message: &str) -> PyErr { PyValueError::new_err(message.to_string()) }
fn offline() -> PyErr { PyConnectionError::new_err("native transport unavailable; endpoint omitted") }

// Only fixed categories and numeric codes cross this boundary: never error text/URLs.
fn diagnostic(phase:&str, started:Instant, status:Option<u16>, received:usize,
              cause:Option<&reqwest::Error>, cancelled:bool, closed:bool)->PyErr {
    let timeout=cause.is_some_and(|e|e.is_timeout());
    let connect=cause.is_some_and(|e|e.is_connect());
    let mut code=None;
    if let Some(error)=cause {
        let mut source=std::error::Error::source(error);
        for _ in 0..16 {
            let Some(error)=source else {break};
            if let Some(io)=error.downcast_ref::<std::io::Error>() {code=io.raw_os_error(); if code.is_some(){break;}}
            source=error.source();
        }
    }
    Python::with_gil(|py| {
        let error=PyConnectionError::new_err("native RPC transport failure; endpoint omitted");
        let value=error.value(py);
        let _=value.setattr("phase",if connect && phase=="request_send" {"connect"} else {phase});
        let _=value.setattr("category",if cancelled {"cancelled"} else if timeout && connect {"connect_timeout"}
            else if timeout {"request_timeout"} else if connect {"connect"} else {"transport"});
        let _=value.setattr("http_status",status);
        let _=value.setattr("elapsed_ms",started.elapsed().as_secs_f64()*1000.0);
        let _=value.setattr("bytes_received",received);
        let _=value.setattr("timeout",timeout);
        let _=value.setattr("connect_error",connect);
        let _=value.setattr("socket_errno",code);
        let _=value.setattr("cancelled",cancelled);
        let _=value.setattr("client_closed",closed);
        let _=value.setattr("pooled_connection_reused",py.None()); // Not observable in reqwest API.
        error
    })
}

fn boundary(inode: u64) -> PyResult<()> {
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::fs::MetadataExt;
        if inode == 0 || std::fs::metadata("/proc/self/ns/net").map_err(|_| offline())?.ino() != inode {
            return Err(bad("approved network namespace mismatch"));
        }
        Ok(())
    }
    #[cfg(not(target_os = "linux"))]
    { let _ = inode; Err(bad("native network requires approved Linux kernel enforcement")) }
}

fn url(value: &str, websocket: bool, loopback: bool) -> PyResult<reqwest::Url> {
    if value.len()>4096 { return Err(bad("endpoint too long")); }
    let parsed = reqwest::Url::parse(value).map_err(|_| bad("invalid endpoint"))?;
    let secure = if websocket { "wss" } else { "https" };
    let local = if websocket { "ws" } else { "http" };
    if parsed.host_str().is_some_and(|s|s.trim_end_matches('.')=="pump.fun" || s.trim_end_matches('.').ends_with(".pump.fun")) {
        return Err(bad("unreviewed web collector denied"));
    }
    let literal_loopback = parsed.host_str().and_then(|s| {
        s.strip_prefix('[').and_then(|v|v.strip_suffix(']')).unwrap_or(s).parse::<std::net::IpAddr>().ok()
    }).is_some_and(|a| a.is_loopback());
    if parsed.host_str().is_none() || !parsed.username().is_empty() || parsed.password().is_some() || parsed.fragment().is_some()
        || !(parsed.scheme()==secure || loopback && literal_loopback && parsed.scheme()==local) {
        return Err(bad("TLS or opted-in literal loopback required"));
    }
    Ok(parsed)
}

fn request(method: &str, params: &str) -> PyResult<String> {
    if !["getGenesisHash","getSlot","getBlocks","getBlock","getSlotLeaders","getClusterNodes","getAccountInfo","getBlockTime","getSignaturesForAddress","getTransaction","getFirstAvailableBlock","minimumLedgerSlot","getHealth","getVersion"].contains(&method) {
        return Err(bad("read-only method allowlist violation"));
    }
    if params.len()>65536 { return Err(bad("request too large")); }
    let params: serde_json::Value = serde_json::from_str(params).map_err(|_| bad("invalid params"))?;
    if !params.is_array() { return Err(bad("params must be array")); }
    Ok(serde_json::json!({"jsonrpc":"2.0","id":1,"method":method,"params":params}).to_string())
}

type HttpReply = (u16,String,String);
type EncodedReply = (u16,String,String,String);
fn block_batch(params:&str)->PyResult<String> {
    if params.len()>65536 {return Err(bad("block batch request too large"));}
    let items:Vec<serde_json::Value>=serde_json::from_str(params).map_err(|_|bad("invalid block batch params"))?;
    if items.is_empty() || items.len()>2 {return Err(bad("block batch must contain one or two calls"));}
    let mut requests=Vec::new();
    for (index,params) in items.into_iter().enumerate() {
        if !params.is_array() {return Err(bad("block params must be array"));}
        requests.push(serde_json::json!({"jsonrpc":"2.0","id":index+1,"method":"getBlock","params":params}));
    }
    Ok(serde_json::Value::Array(requests).to_string())
}
fn http_timing(timing:&std::sync::Mutex<serde_json::Value>, phase:&str,
    started:Instant, headers:Option<Instant>, received:usize) {
    if let Ok(mut value)=timing.lock() {
        *value=serde_json::json!({"transport_phase":phase,
            "transport_elapsed_ms":started.elapsed().as_secs_f64()*1000.0,
            "request_headers_ms":headers.map(|at|at.duration_since(started).as_secs_f64()*1000.0),
            "response_body_ms":headers.map(|at|at.elapsed().as_secs_f64()*1000.0),
            "bytes_received":received});
    }
}
#[pyclass]
struct NativeHttp {
    client: std::sync::Mutex<Option<reqwest::Client>>, endpoint: reqwest::Url,
    inode:u64, busy:Arc<Semaphore>, stop:tokio::sync::watch::Sender<bool>,
    timing:Arc<std::sync::Mutex<serde_json::Value>>,
}
impl NativeHttp {
    fn operation(&self,method:&str,params:&str,compressed:bool)->PyResult<impl std::future::Future<Output=PyResult<EncodedReply>>+Send+'static> {
        self.operation_body(request(method,params)?,compressed)
    }
    fn operation_body(&self,body:String,compressed:bool)->PyResult<impl std::future::Future<Output=PyResult<EncodedReply>>+Send+'static> {
        boundary(self.inode)?;
        let started=Instant::now();
        let client=self.client.lock().map_err(|_|diagnostic("client_state",started,None,0,None,false,false))?
            .as_ref().ok_or_else(||diagnostic("client_state",started,None,0,None,false,true))?.clone();
        let endpoint=self.endpoint.clone(); let mut stop=self.stop.subscribe();
        if *stop.borrow() {return Err(diagnostic("cancellation",started,None,0,None,true,true));}
        let permit=self.busy.clone().try_acquire_owned().map_err(|_|PyRuntimeError::new_err("HTTP backpressure: one request already active"))?;
        let timing=self.timing.clone();
        http_timing(&timing,"request_send",started,None,0);
        Ok(async move {
            let _permit=permit;
            http_timing(&timing,"request_send",started,None,0);
            let mut headers_at=None;
            let status_seen=std::sync::atomic::AtomicU16::new(0);
            let received=std::sync::atomic::AtomicUsize::new(0);
            let result=tokio::select! {
                biased;
                _=stop.changed()=>Err(diagnostic("cancellation",started,
                    match status_seen.load(std::sync::atomic::Ordering::Relaxed) {0=>None,n=>Some(n)},
                    received.load(std::sync::atomic::Ordering::Relaxed),None,true,true)),
                result=async {
                    let mut response=client.post(endpoint).header("Content-Type","application/json")
                        .header("Accept-Encoding",if compressed {"gzip"} else {"identity"}).body(body).send().await.map_err(|e|diagnostic("request_send",started,None,0,Some(&e),false,false))?;
                    headers_at=Some(Instant::now());
                    http_timing(&timing,"response_body",started,headers_at,0);
                    let status=response.status().as_u16();
                    status_seen.store(status,std::sync::atomic::Ordering::Relaxed);
                    let retry=response.headers().get("retry-after").and_then(|v|v.to_str().ok()).unwrap_or("").chars().take(256).collect::<String>();
                    if status!=200 {return Ok((status,retry,"identity".to_string(),String::new()));}
                    if response.content_length().is_some_and(|n|n>64_000_000) {return Err(bad("oversized response"));}
                    let encoding=response.headers().get("content-encoding").and_then(|v|v.to_str().ok()).unwrap_or("identity").to_string();
                    if encoding!="identity" && !(compressed && encoding=="gzip") {return Err(bad("unsupported native response encoding"));}
                    // Bound eager allocation even when a peer advertises a large body.
                    // Normal block responses avoid repeatedly growing from zero.
                    let capacity=response.content_length().unwrap_or(0).min(1_048_576) as usize;
                    let mut bytes=Vec::with_capacity(capacity);
                    while let Some(chunk)=response.chunk().await.map_err(|e|diagnostic("response_body",started,Some(status),bytes.len(),Some(&e),false,false))? {
                        if bytes.len()+chunk.len()>64_000_000 {return Err(bad("oversized response"));}
                        bytes.extend_from_slice(&chunk);
                        received.store(bytes.len(),std::sync::atomic::Ordering::Relaxed);
                        http_timing(&timing,"response_body",started,headers_at,bytes.len());
                    }
                    // Release the completed body before the single-flight permit;
                    // let Hyper return its connection to the shared runtime's pool.
                    drop(response);
                    tokio::task::yield_now().await;
                    let text=if encoding=="gzip" {base64::engine::general_purpose::STANDARD.encode(bytes)}
                        else {String::from_utf8(bytes).map_err(|_|bad("invalid response UTF8"))?};
                    Ok((status,retry,encoding,text))
                }=>result,
            };
            http_timing(&timing,if result.is_ok(){"transport_complete"}
                else if headers_at.is_some(){"response_body"}else{"request_send"},
                started,headers_at,received.load(std::sync::atomic::Ordering::Relaxed));
            result
        })
    }
}
#[pymethods]
impl NativeHttp {
    #[new]
    #[pyo3(signature=(endpoint,namespace_inode,allow_loopback,tcp_nodelay=None))]
    fn new(endpoint:&str,namespace_inode:u64,allow_loopback:bool,tcp_nodelay:Option<bool>)->PyResult<Self> {
        boundary(namespace_inode)?;
        let endpoint=url(endpoint,false,allow_loopback)?;
        // Bind pooled connections/timers to the one persistent Tokio runtime,
        // never to an ephemeral Python asyncio loop or caller's runtime.
        let _runtime=pyo3_async_runtimes::tokio::get_runtime().enter();
        let mut builder=reqwest::Client::builder().redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(30)).connect_timeout(Duration::from_secs(15))
            .pool_max_idle_per_host(1).pool_idle_timeout(Duration::from_secs(30)).no_proxy();
        // Benchmark-only opt-in. None preserves the existing production default.
        if let Some(enabled)=tcp_nodelay {builder=builder.tcp_nodelay(enabled);}
        let started=Instant::now();
        let client=builder.build().map_err(|e|diagnostic("client_state",started,None,0,Some(&e),false,false))?;
        let (stop,_)=tokio::sync::watch::channel(false);
        Ok(Self {client:std::sync::Mutex::new(Some(client)),endpoint,inode:namespace_inode,busy:Arc::new(Semaphore::new(1)),stop,
            timing:Arc::new(std::sync::Mutex::new(serde_json::json!({})))})
    }
    fn request<'py>(&self,py:Python<'py>,method:&str,params:&str)->PyResult<Bound<'py,PyAny>> {
        let operation=self.operation(method,params,false)?;
        pyo3_async_runtimes::tokio::future_into_py(py,async move {
            let (status,retry,_,text)=operation.await?; Ok((status,retry,text))
        })
    }
    fn timing_snapshot(&self)->PyResult<String> {
        Ok(self.timing.lock().map_err(|_|offline())?.to_string())
    }
    fn request_sync(&self,py:Python<'_>,method:&str,params:&str)->PyResult<HttpReply> {
        if tokio::runtime::Handle::try_current().is_ok() {return Err(PyRuntimeError::new_err("blocking RPC inside Tokio is forbidden"));}
        let operation=self.operation(method,params,false)?;
        let (status,retry,_,text)=py.allow_threads(move ||pyo3_async_runtimes::tokio::get_runtime().block_on(operation))?;
        Ok((status,retry,text))
    }
    fn request_encoded<'py>(&self,py:Python<'py>,method:&str,params:&str)->PyResult<Bound<'py,PyAny>> {
        pyo3_async_runtimes::tokio::future_into_py(py,self.operation(method,params,true)?)
    }
    fn request_encoded_sync(&self,py:Python<'_>,method:&str,params:&str)->PyResult<EncodedReply> {
        if tokio::runtime::Handle::try_current().is_ok() {return Err(PyRuntimeError::new_err("blocking RPC inside Tokio is forbidden"));}
        let operation=self.operation(method,params,true)?;
        py.allow_threads(move ||pyo3_async_runtimes::tokio::get_runtime().block_on(operation))
    }
    fn request_blocks_encoded_sync(&self,py:Python<'_>,params:&str)->PyResult<EncodedReply> {
        if tokio::runtime::Handle::try_current().is_ok() {return Err(PyRuntimeError::new_err("blocking RPC inside Tokio is forbidden"));}
        let operation=self.operation_body(block_batch(params)?,true)?;
        py.allow_threads(move ||pyo3_async_runtimes::tokio::get_runtime().block_on(operation))
    }
    fn close(&self)->PyResult<()> {
        self.stop.send_replace(true);
        self.client.lock().map_err(|_|offline())?.take();
        Ok(())
    }
}

type Socket = WebSocketStream<MaybeTlsStream<tokio::net::TcpStream>>;
#[pyclass]
struct NativeSocket {
    sink: Arc<Mutex<Option<futures_util::stream::SplitSink<Socket,Message>>>>,
    stream: Arc<Mutex<Option<futures_util::stream::SplitStream<Socket>>>>,
    send_busy: Arc<Semaphore>, recv_busy: Arc<Semaphore>,
    stop: tokio::sync::watch::Sender<bool>, inode: u64,
}
#[pymethods]
impl NativeSocket {
    fn send<'py>(&self, py: Python<'py>, text: String) -> PyResult<Bound<'py, PyAny>> {
        boundary(self.inode)?;
        if text.len()>65536 { return Err(bad("subscription too large")); }
        let value:serde_json::Value=serde_json::from_str(&text).map_err(|_|bad("invalid subscription"))?;
        if value["jsonrpc"]!="2.0" || value["method"]!="logsSubscribe" { return Err(bad("only logsSubscribe is supported")); }
        let sink=self.sink.clone(); let mut stop=self.stop.subscribe();
        let permit=self.send_busy.clone().try_acquire_owned().map_err(|_|PyRuntimeError::new_err("WS send backpressure"))?;
        pyo3_async_runtimes::tokio::future_into_py(py,async move {
            let _permit=permit;
            if *stop.borrow() { return Err(offline()); }
            tokio::select! {
                biased;
                _=stop.changed()=>Err(offline()),
                result=tokio::time::timeout(Duration::from_secs(15),async {
                    let mut guard=sink.lock().await;
                    guard.as_mut().ok_or_else(offline)?.send(Message::Text(text.into())).await.map_err(|_|offline())
                })=>result.map_err(|_|offline())?,
            }
        })
    }
    fn recv<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        boundary(self.inode)?;
        let stream=self.stream.clone(); let sink=self.sink.clone(); let mut stop=self.stop.subscribe();
        let permit=self.recv_busy.clone().try_acquire_owned().map_err(|_|PyRuntimeError::new_err("WS receive backpressure"))?;
        pyo3_async_runtimes::tokio::future_into_py(py,async move {
            let _permit=permit;
            if *stop.borrow() { return Err(offline()); }
            tokio::select! {
                biased;
                _=stop.changed()=>Err(offline()),
                result=async {
                    let mut guard=stream.lock().await;
                    let ws=guard.as_mut().ok_or_else(offline)?;
                    let mut controls=0;
                    let mut control_window=tokio::time::Instant::now();
                    loop {
                        let message=match tokio::time::timeout(Duration::from_secs(20),ws.next()).await {
                            Ok(message)=>message,
                            Err(_)=>{
                                controls=0;
                                let mut output=sink.lock().await;
                                tokio::time::timeout(Duration::from_secs(10),output.as_mut().ok_or_else(offline)?.send(Message::Ping(b"solana-observer".to_vec().into())))
                                    .await.map_err(|_|offline())?.map_err(|_|offline())?;
                                drop(output);
                                tokio::time::timeout(Duration::from_secs(10),ws.next()).await.map_err(|_|offline())?
                            },
                        };
                        match message {
                            Some(Ok(Message::Text(text)))=>return Ok(text.to_string()),
                            Some(Ok(Message::Ping(data)))=>{
                                let mut output=sink.lock().await;
                                tokio::time::timeout(Duration::from_secs(10),output.as_mut().ok_or_else(offline)?.send(Message::Pong(data)))
                                    .await.map_err(|_|offline())?.map_err(|_|offline())?;
                            },
                            Some(Ok(Message::Pong(_)))=>(),
                            _=>return Err(offline()),
                        }
                        if control_window.elapsed()>=Duration::from_secs(1) {
                            controls=0;control_window=tokio::time::Instant::now();
                        }
                        controls+=1;
                        if controls>16 {return Err(bad("excessive websocket control frames"));}
                    }
                }=>result,
            }
        })
    }
    fn close<'py>(&self,py:Python<'py>)->PyResult<Bound<'py,PyAny>> {
        // Signal before constructing the awaitable: canceling close cannot undo it.
        self.stop.send_replace(true);
        let sink=self.sink.clone(); let stream=self.stream.clone();
        pyo3_async_runtimes::tokio::future_into_py(py,async move {
            sink.lock().await.take(); stream.lock().await.take(); Ok(())
        })
    }
}

#[pyfunction]
fn connect_ws<'py>(py: Python<'py>, endpoint: &str, namespace_inode: u64, allow_loopback: bool) -> PyResult<Bound<'py, PyAny>> {
    boundary(namespace_inode)?;
    let endpoint=url(endpoint,true,allow_loopback)?.to_string();
    pyo3_async_runtimes::tokio::future_into_py(py,async move {
        let mut config=WebSocketConfig::default();
        config.max_message_size=Some(2_000_000); config.max_frame_size=Some(2_000_000);
        config.write_buffer_size=65536; config.max_write_buffer_size=262144;
        let (ws,_)=tokio::time::timeout(Duration::from_secs(15),connect_async_with_config(endpoint,Some(config),true)).await.map_err(|_| offline())?.map_err(|error| match error {
            tokio_tungstenite::tungstenite::Error::Http(response) => {
                let retry=response.headers().get("retry-after").and_then(|v|v.to_str().ok()).unwrap_or("");
                bad(&serde_json::json!({"ws_http_status":response.status().as_u16(),"retry_after":retry.chars().take(256).collect::<String>()}).to_string())
            },
            _ => offline(),
        })?;
        let (sink,stream)=ws.split();
        let (stop,_)=tokio::sync::watch::channel(false);
        Python::with_gil(|py|Py::new(py,NativeSocket {
            sink:Arc::new(Mutex::new(Some(sink))),stream:Arc::new(Mutex::new(Some(stream))),
            send_busy:Arc::new(Semaphore::new(1)),recv_busy:Arc::new(Semaphore::new(1)),stop,inode:namespace_inode,
        }))
    })
}

/// Offline replay summary, not market events or finalized verification.
#[pyfunction]
fn replay_binary_response(text: &str) -> PyResult<Vec<(usize,String,Option<bool>,Option<i16>)>> {
    use base64::Engine;
    if text.len()>64_000_000 { return Err(bad("oversized replay")); }
    let value:serde_json::Value=serde_json::from_str(text).map_err(|_| bad("invalid replay JSON"))?;
    if value["jsonrpc"]!="2.0" || value["id"].as_u64()!=Some(1) || value.get("error").is_some() {
        return Err(bad("invalid replay envelope"));
    }
    let result=value.get("result").ok_or_else(||bad("missing replay result"))?;
    let items:Vec<&serde_json::Value>=if let Some(items)=result["transactions"].as_array() {
        if items.len()>100_000 { return Err(bad("too many replay transactions")); }
        items.iter().collect()
    } else if result.get("transaction").is_some() { vec![result] }
    else { return Err(bad("no replay transactions")); };
    let mut rows=Vec::with_capacity(items.len());
    for (index,item) in items.iter().enumerate() {
        if !item.is_object() { rows.push((index,"quarantined".into(),None,None)); continue; }
        let tx=&item["transaction"];
        if tx.as_array().map(|v|v.len())!=Some(2) || tx.get(1).and_then(|v|v.as_str())!=Some("base64") {
            rows.push((index,"unsupported_json".into(),None,None)); continue;
        }
        let raw=tx.get(0).and_then(|v|v.as_str()).filter(|s|s.len()<=1644)
            .and_then(|s|base64::engine::general_purpose::STANDARD.decode(s).ok());
        let parsed=raw.as_ref().and_then(|r|super::parse(r).ok());
        let err=item.get("meta").and_then(|m|m.get("err"));
        if let (Some(parsed),Some(err))=(parsed,err) {
            if err.is_null() || err.is_object() || err.is_string() {
                rows.push((index,"parsed_unverified".into(),Some(err.is_null()),Some(parsed.0))); continue;
            }
        }
        rows.push((index,"quarantined".into(),None,None));
    }
    Ok(rows)
}

pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    let mut runtime=tokio::runtime::Builder::new_multi_thread();
    runtime.worker_threads(2).max_blocking_threads(2).thread_stack_size(512*1024).enable_all();
    pyo3_async_runtimes::tokio::init(runtime);
    module.add_function(wrap_pyfunction!(replay_binary_response,module)?)?;
    module.add_class::<super::borsh::NativeBorshSchema>()?;
    module.add_class::<NativeHttp>()?;
    module.add_function(wrap_pyfunction!(connect_ws,module)?)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test] fn http_timing_snapshot_contains_only_phase_and_numbers() {
        let timing=std::sync::Mutex::new(serde_json::json!({}));
        let started=Instant::now();
        http_timing(&timing,"response_body",started,Some(started),123);
        let value=timing.lock().unwrap();
        assert_eq!(value["transport_phase"],"response_body");assert_eq!(value["bytes_received"],123);
        assert!(value["request_headers_ms"].as_f64().unwrap()>=0.0);
        assert!(value["response_body_ms"].as_f64().unwrap()>=0.0);
        assert_eq!(value.as_object().unwrap().len(),5);
    }
    #[test] fn request_timeout_diagnostics_are_redacted() {
        // Local listener intentionally never sends headers; no external connection.
        let listener=std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let endpoint=format!("http://{}/SECRET-key",listener.local_addr().unwrap());
        let runtime=tokio::runtime::Runtime::new().unwrap();
        let cause=runtime.block_on(async {
            reqwest::Client::builder().no_proxy().timeout(Duration::from_millis(200))
                .connect_timeout(Duration::from_millis(100)).build().unwrap()
                .get(endpoint).send().await.unwrap_err()
        });
        assert!(cause.is_timeout());
        assert!(!cause.is_connect());
        pyo3::prepare_freethreaded_python();
        let error=diagnostic("request_send",Instant::now(),None,0,Some(&cause),false,false);
        Python::with_gil(|py| {
            assert!(!error.to_string().contains("SECRET"));
            assert_eq!(error.value(py).getattr("category").unwrap().extract::<String>().unwrap(),"request_timeout");
            assert!(error.value(py).getattr("timeout").unwrap().extract::<bool>().unwrap());
            assert!(error.value(py).getattr("http_status").unwrap().is_none());
        });
    }

    #[test] fn tls_connect_timeout_is_distinct_from_request_timeout() {
        let listener=std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let endpoint=format!("https://{}/SECRET-key",listener.local_addr().unwrap());
        let runtime=tokio::runtime::Runtime::new().unwrap();
        // TCP establishes locally, but no TLS handshake response ever arrives.
        let cause=runtime.block_on(async {
            reqwest::Client::builder().no_proxy().timeout(Duration::from_secs(1))
                .connect_timeout(Duration::from_millis(100)).build().unwrap()
                .get(endpoint).send().await.unwrap_err()
        });
        assert!(cause.is_timeout());assert!(cause.is_connect());
        pyo3::prepare_freethreaded_python();
        let error=diagnostic("request_send",Instant::now(),None,0,Some(&cause),false,false);
        Python::with_gil(|py| {
            assert_eq!(error.value(py).getattr("category").unwrap().extract::<String>().unwrap(),"connect_timeout");
            assert_eq!(error.value(py).getattr("phase").unwrap().extract::<String>().unwrap(),"connect");
            assert!(!error.to_string().contains("SECRET"));
        });
    }

    #[test] fn bounded_block_batch_is_read_only() {
        let body=block_batch("[[10,{}],[11,{}]]").unwrap();
        let calls:serde_json::Value=serde_json::from_str(&body).unwrap();
        assert_eq!(calls[0]["method"],"getBlock");
        assert_eq!(calls[1]["method"],"getBlock");
        assert_eq!(calls[0]["id"],1);
        assert_eq!(calls[1]["id"],2);
        for invalid in ["[]","[[],[],[]]","[null]","[{\"method\":\"sendTransaction\"}]"] {
            assert!(block_batch(invalid).is_err());
        }
    }
    #[test] fn read_only_requests() {
        assert!(request("sendTransaction","[]").is_err());
        assert!(request("getSlot","{}").is_err());
        let value:serde_json::Value=serde_json::from_str(&request("getSlot","[]").unwrap()).unwrap();
        assert_eq!(value["id"],1);
    }
    #[test] fn backpressure_is_bounded() {
        let semaphore=Arc::new(Semaphore::new(1));
        let permit=semaphore.clone().try_acquire_owned().unwrap();
        assert!(semaphore.clone().try_acquire_owned().is_err());
        drop(permit);
        assert!(semaphore.try_acquire_owned().is_ok());
    }
    #[test] fn independent_directions_and_persistent_shutdown() {
        let send=Arc::new(Semaphore::new(1)); let recv=Arc::new(Semaphore::new(1));
        let sending=send.clone().try_acquire_owned().unwrap();
        let receiving=recv.clone().try_acquire_owned().unwrap();
        assert!(send.clone().try_acquire_owned().is_err());
        assert!(recv.clone().try_acquire_owned().is_err());
        let (stop,_)=tokio::sync::watch::channel(false);
        stop.send_replace(true);
        assert!(*stop.subscribe().borrow());
        drop((sending,receiving));
        assert!(send.try_acquire_owned().is_ok());
        assert!(recv.try_acquire_owned().is_ok());
    }
    #[test] fn endpoints() {
        assert!(url("http://example.org",false,true).is_err());
        assert!(url("http://127.0.0.1:8899",false,true).is_ok());
        assert!(url("http://[::1]:8899",false,true).is_ok());
        assert!(url("https://pump.fun.",false,false).is_err());
        assert!(url("https://user:secret@example.org",false,false).is_err());
    }
}
