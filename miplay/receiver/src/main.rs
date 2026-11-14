use fusionplay_miplay_sdk::{MiPlayDeviceType, MiPlayReceiver, ReceiverConfig, ReceiverIdentity};
use std::{
    env,
    io::{self, Write},
    sync::{Arc, Mutex},
    time::Duration,
};

fn main() -> anyhow::Result<()> {
    let identity = ReceiverIdentity::new(
        env::var("MIPLAY_IDM_ID")?,
        env::var("MIPLAY_LYRA_ID")?,
        env::var("MIPLAY_MEDIA_ID")?,
    );
    let config = ReceiverConfig::new(
        env::var("MIPLAY_NAME")?,
        identity,
        env::var("MIPLAY_HOST")?.parse()?,
        env::var("MIPLAY_INTERFACE")?,
    )
    .with_device_type(MiPlayDeviceType::Speaker)
    .with_initial_volume_percent(50);
    let writer = Arc::new(Mutex::new(io::stdout()));
    let events = Arc::new(move |value: serde_json::Value| {
        // Keep PCM on a local pipe, never in Docker logs.
        if let Ok(mut output) = writer.lock() {
            let _ = writeln!(output, "{}", value);
            let _ = output.flush();
        }
    });
    let _receiver = MiPlayReceiver::start(config, events)?;
    loop {
        std::thread::sleep(Duration::from_secs(1));
    }
}
