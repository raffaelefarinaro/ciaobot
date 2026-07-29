use serde_json::Value;
use std::{
    fs::File,
    io::{self, Read, Seek, SeekFrom},
    path::PathBuf,
};

#[derive(Debug)]
pub struct NotificationLogTail {
    path: PathBuf,
    position: u64,
    partial: String,
}

impl NotificationLogTail {
    pub fn at_end(path: impl Into<PathBuf>) -> io::Result<Self> {
        let path = path.into();
        let position = path.metadata().map(|metadata| metadata.len()).unwrap_or(0);
        Ok(Self {
            path,
            position,
            partial: String::new(),
        })
    }

    pub fn poll(&mut self) -> io::Result<Vec<Value>> {
        let Ok(mut file) = File::open(&self.path) else {
            self.position = 0;
            self.partial.clear();
            return Ok(Vec::new());
        };
        let length = file.metadata()?.len();
        if length < self.position {
            self.position = 0;
            self.partial.clear();
        }
        file.seek(SeekFrom::Start(self.position))?;
        let mut appended = String::new();
        file.read_to_string(&mut appended)?;
        self.position = file.stream_position()?;
        self.partial.push_str(&appended);

        let mut values = Vec::new();
        while let Some(newline) = self.partial.find('\n') {
            let line = self.partial[..newline].trim().to_string();
            self.partial.drain(..=newline);
            if line.is_empty() {
                continue;
            }
            if let Ok(value) = serde_json::from_str(&line) {
                values.push(value);
            }
        }
        Ok(values)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{fs, io::Write};
    use tempfile::tempdir;

    #[test]
    fn starts_at_end_and_processes_partial_lines_once() {
        let temp = tempdir().unwrap();
        let path = temp.path().join("notifications.jsonl");
        fs::write(&path, "{\"title\":\"old\"}\n").unwrap();
        let mut tail = NotificationLogTail::at_end(&path).unwrap();
        let mut file = fs::OpenOptions::new().append(true).open(&path).unwrap();
        write!(file, "{{\"title\":\"new\"}}").unwrap();
        assert!(tail.poll().unwrap().is_empty());
        writeln!(file).unwrap();
        let values = tail.poll().unwrap();
        assert_eq!(values.len(), 1);
        assert_eq!(values[0]["title"], "new");
        assert!(tail.poll().unwrap().is_empty());
    }

    #[test]
    fn handles_truncation_and_malformed_rows() {
        let temp = tempdir().unwrap();
        let path = temp.path().join("notifications.jsonl");
        fs::write(&path, "old bytes that make the initial offset long").unwrap();
        let mut tail = NotificationLogTail::at_end(&path).unwrap();
        fs::write(&path, "bad\n{\"title\":\"after rotate\"}\n").unwrap();
        let values = tail.poll().unwrap();
        assert_eq!(values.len(), 1);
        assert_eq!(values[0]["title"], "after rotate");
    }
}
