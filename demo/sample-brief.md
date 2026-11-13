# Research brief: Write-ahead logging in SQLite

Source: https://sqlite.org/wal.html  
Model: nvidia/nemotron-3.5-lightning:free (draft), nvidia/nemotron-3.5-lightning:free (critique)

**Summary**  
SQLite's Write-Ahead Log (WAL) mode, available since version 3.7.0, replaces the default rollback journal by preserving original database content and appending changes to a separate WAL file. A COMMIT occurs when a special record is appended to the WAL, allowing the main database to remain unaltered and enabling concurrent read and write operations.

**Key points**  
- Readers and writers operate concurrently; neither blocks the other, allowing simultaneous reading and writing.  
- Disk I/O tends to be more sequential, and fewer fsync() calls make WAL less vulnerable to issues on systems where fsync is broken.  
- All processes using the database must reside on the same host, as WAL requires shared memory and cannot function over a network filesystem.  
- Each database accumulates persistent "-wal" and "-shm" shared-memory files, which can make SQLite less appealing as a general application file format.  
- Automatic checkpoints occur by default when the WAL reaches 1000 pages, with the threshold configurable or disableable via compile-time or runtime options.  
- Only one writer can operate at a time due to the single WAL file, though multiple readers are supported.

**Trade-offs**  
- WAL improves concurrency and I/O efficiency but adds persistent files and a checkpoint mechanism that can be stalled by long-running read transactions.  
- The page size cannot be changed while in WAL mode; rollback journal mode must be used for such modifications.  
- Very large transactions (exceeding 100 MB or 1 GB) may perform poorly or fail in WAL mode, though SQLite 3.11.0+ improved efficiency for large transactions.  
- WAL incurs only a ~1–2% performance overhead in read-heavy workloads compared to the traditional rollback-journal approach.

**Open questions**  
- How should application developers optimally tune the automatic checkpoint threshold and manual checkpoint scheduling for specific workloads?  
- What are the recovery procedures and implications when "-wal" or "-shm" files are corrupted, missing, or improperly closed?  
- Can WAL mode be reliably deployed in environments with constrained or virtualized shared memory, such as containers or network-filesystem-proximate setups?

---

## Reviewer notes (critique step)

- The claim that “Readers and writers operate concurrently; neither blocks the other, allowing simultaneous reading and writing” is unsupported because SOURCE explicitly states that only one writer can operate at a time due to the single WAL file, meaning writers do block other writers.  
- The assertion that “Very large transactions (exceeding 100 MB or 1 GB) may perform poorly or fail in WAL mode, though SQLite 3.11.0+ improved efficiency for large transactions” misrepresents the source, which states that beginning with 3.11.0, WAL mode works as efficiently as rollback mode for large transactions, contradicting the notion that they may perform poorly or fail.  
- SOURCE does not describe recovery procedures or implications when “-wal” or “-shm” files are corrupted, missing, or improperly closed; the DRAFT’s open question on this topic highlights an area not addressed by the source.  
- Important omission: DRAFT does not mention that it is not possible to open read-only WAL databases without write privileges for the “-shm” shared‑memory file or the containing directory, as detailed in SOURCE beginning with version 3.22.0.  
- Important omission: DRAFT does not mention that transactions involving multiple ATTACHed databases are atomic for each individual database but are not atomic across all databases as a set, as stated in SOURCE.
