# Strategi Ingestion: Analisis SLM-based VDU dan Opsi Implementasi OCR/Vision

**Dokumen Arsitektur & Diskusi Desain**  
**Tanggal:** 24 November 2026  
**Referensi:** [The Neural Maze — The SLM-OCR Course: The $10,000 OCR Pipeline We're Giving Away Free](https://www.theneuralmaze.com/p/the-slm-ocr-course-starts-now-the)  
**Status:** Draf Diskusi untuk Milestone Minggu 2 (`src/lunas/ingest/`)

---

## 1. Latar Belakang & Konteks

Dalam roadmap Minggu 2 `lunas` (SPEC §4a dan §12), komponen ingestion dokumen direncanakan memiliki tiga backend (`pdf_text`, `ocr`, `vision`) di bawah router cerdas deterministik (`auto`). 

Sebelum mengimplementasikan `ocr.py` dan `vision.py`, artikel dari **The Neural Maze** memberikan perspektif industri terkini mengenai pergeseran paradigma dari *Traditional OCR* ke **SLM-based Visual Document Understanding (VDU)**. Dokumen ini merangkum temuan kunci tersebut dan memetakannya ke dalam opsi-opsi implementasi untuk arsitektur `lunas`.

---

## 2. Temuan Kunci dari Artikel The Neural Maze

### A. Keterbatasan Fatal "Traditional OCR" pada Dokumen Finansial
1. **Hilangnya Struktur Spasial (*Spatial Blindness*)**:
   - Engine OCR klasik (seperti Tesseract atau character-level detector) hanya menghasilkan *flat string* atau dump karakter baris per baris.
   - Pada invoice yang memiliki layout kompleks (mis. tabel banyak kolom, rincian PPN di sebelah kanan, dan informasi rekening di sebelah kiri), OCR tradisional sering menggabungkan teks horizontal lintas kolom menjadi satu kalimat rusak.
2. **Dampak ke Downstream LLM**:
   - Ketika teks yang rusak dan acak-acakan ini dimasukkan ke node ekstraksi, LLM kesulitan mengasosiasikan nama barang, kuantitas, harga satuan, dan subtotal secara benar.

### B. Dilema Mengandalkan Frontier Multi-Modal VLM (GPT-4o, Claude 3.5 Sonnet)
1. **Biaya Tinggi**: Mengirim setiap halaman gambar ke frontier model memakan biaya \$10–\$30+ per 1.000 dokumen—tidak ekonomis untuk operasional akun hutang (*Accounts Payable*) volume tinggi.
2. **Latensi**: Panggilan multimodal ke model besar membutuhkan 3–6 detik per halaman.
3. **Kepatuhan & Privasi (Compliance/PII)**: Mengirim invoice dan rekening vendor pihak ketiga ke API cloud proprietary sering dilarang oleh kebijakan keamanan perbankan/enterprise.

### C. Standar Industri Modern: SLM-based VDU (Small Language Models)
1. **Titik Manis (*Sweet Spot*)**:
   - Model Vision-Language kompak (2B–7B parameter, seperti **Qwen2.5-VL**, SmolVLM, atau parser berbasis vision layout seperti Docling).
2. **Visual Grounding & Rekonstruksi Struktur**:
   - Model SLM VDU dilatih untuk memahami *reading order*, hierarki heading, dan relasi tabel, lalu mentranskripsikannya ke dalam representasi **Markdown / HTML tables** yang bersih secara *verbatim*.
3. **Efisiensi**:
   - Dapat di-host secara lokal / *on-premise* (menggunakan vLLM / ONNX / Ollama), dengan latensi rendah dan biaya operasional mendekati nol.

---

## 3. Penyelarasan dengan Aturan Desain `lunas` (SPEC §4a)

Filosofi dalam artikel The Neural Maze **100% konsisten** dengan invariant arsitektur yang sudah kita tetapkan di `SPEC.md`:

1. **Abstraksi Bersama (`ParsedDocument`)**:
   Semua backend (`pdf_text`, `ocr`, `vision`) wajib mengembalikan tipe data yang sama:
   ```python
   class ParsedDocument(BaseModel):
       pages: list[Page]  # number, text, tables
       backend: Backend  # "pdf_text" | "ocr" | "vision"
       confidence: float
   ```
2. **Transkripsi Murni, Bukan Ekstraksi Terpadu**:
   - `VisionReader` dan `OcrReader` **hanya bertugas mentranskripsi halaman gambar ke teks dan tabel markdown**.
   - Backend ingestion **dilarang langsung mengisi skema `Invoice`**.
   - Tanggung jawab ekstraksi tetap berada di node `extract_invoice` (Node 3) agar evaluasi performa, benchmark key-field, dan guardrail validasi berlaku seragam di semua jenis dokumen.

---

## 4. Opsi Implementasi untuk `lunas`

Berikut tiga opsi implementasi yang dapat dipertimbangkan:

### Opsi 1: Tiering Pragmatis (RapidOCR + Cloud SLM Transcriber) — *Direkomendasikan*
Pendekatan berjenjang yang memanfaatkan keunggulan masing-masing layer tanpa menambah beban infrastruktur lokal:
1. **`pdf_text.py`**: Memakai `pdfplumber` untuk PDF digital (gratis, latensi milidetik).
2. **`ocr.py`**: Memakai `rapidocr-onnxruntime` (ringan, pure Python/ONNX, jalan native di Apple Silicon tanpa dependensi C eksternal seperti binary Tesseract). Digunakan untuk dokumen scan/gambar yang bersih.
3. **`vision.py`**: Memakai SLM Vision via provider API yang sudah ada (`glm-5.3-flash` di Sumopod). 
   - Dilengkapi prompt transkripsi khusus VDU: *"Transcribe this invoice image into clean Markdown, preserving all table columns and key-value alignments verbatim without extracting or summarizing."*
4. **Router `auto`**:
   - $\ge 50$ karakter teks digital per halaman $\to$ `pdf_text`.
   - Di bawah 50 karakter $\to$ coba `ocr`.
   - Jika confidence OCR rendah atau validasi awal gagal $\to$ fallback ke `vision`.

* **Kelebihan**: Cepat selesai dalam sprint Minggu 2, tidak membutuhkan VRAM GPU lokal besar, biaya API sangat murah (saldo \$2.88 masih sangat leluasa).
* **Kekurangan**: Tier `ocr` lokal masih memiliki keterbatasan khas OCR klasik pada tabel rumit.

---

### Opsi 2: Full Local SLM-VLM (Qwen2.5-VL via Ollama / Local Serving)
Mengadopsi artikel secara penuh dengan menjalankan SLM Vision lokal di mesin pengembang (Mac Mini M4):
1. Mengunduh model open-weights Qwen2.5-VL (3B atau 7B) dan menjalankannya melalui Ollama atau MLX/vLLM lokal.
2. `vision.py` memanggil endpoint lokal tersebut untuk mentranskripsi gambar ke Markdown tabel.

* **Kelebihan**: 100% lokal, nol biaya API, bebas isu privasi/kepatuhan.
* **Kekurangan**: 
  - Membutuhkan download bobot model 4–8 GB.
  - Membebani memori terpadu (RAM) Mac saat menjalankan evaluasi batch (145–155 invoice).
  - Latensi per dokumen di Mac lokal (~2–4 detik) bisa lebih lambat dibanding API provider cloud paralel (4 workers).

---

### Opsi 3: SLM-First VDU (Bypass Traditional OCR)
Menyederhanakan pipeline dengan menghilangkan ketergantungan pada OCR klasik:
1. **Hanya 2 Backend**:
   - `pdf_text`: Untuk digital PDF dengan text layer memadai.
   - `vision` (SLM-VDU): Untuk semua dokumen scan atau gambar, langsung ditranskripsi oleh SLM Vision ke tabel Markdown.
2. Mengeliminasi `rapidocr`/`tesseract` sepenuhnya karena format invoice didominasi tabel yang rentan rusak oleh OCR klasik.

* **Kelebihan**: Menghindari kompleksitas pemeliharaan engine OCR klasik dan potensi kegagalan tabel; transkripsi selalu mempertahankan relasi spasial.
* **Kekurangan**: Setiap dokumen scan selalu memakan biaya token vision API (meski relatif kecil dengan SLM).

---

## 5. Rencana Aksi & Diskusi Lanjutan dengan Claude

Untuk melanjutkan implementasi bersama Claude, langkah-langkah konkret yang diusulkan:

1. **Sepakati Opsi Ingestion**:
   - Apakah menyetujui **Opsi 1** (paling seimbang untuk portofolio dan kecepatan delivery Minggu 2) atau ingin mencoba **Opsi 3** / **Opsi 2**?
2. **Implementasi Komponen Pendukung**:
   - **Prompt Transkripsi VDU**: Buat `prompts/transcribe_vision.v1.md` yang menginstruksikan model untuk menghasilkan tabel Markdown murni.
   - **Generator Invoice Scanned/Noisy**: Buat script pembantu (mis. `data/generate_scans.py`) yang merender ~25% invoice PDF menjadi gambar dengan sedikit noise/rotasi untuk dataset pengujian.
   - **Implementasi `ocr.py` & `vision.py`**: Lengkapi stub yang ada di `src/lunas/ingest/`.
   - **Unit Tests & Evals**: Tambahkan pengujian di `tests/test_ingest.py` dan eval transkripsi/ekstraksi pada dokumen scan.
