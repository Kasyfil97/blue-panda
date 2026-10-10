# Evaluasi Hierarki Resolver: v3 sampai v6

Dokumen ini menjelaskan metode, hasil, dan kekurangan empat putaran evaluasi hierarki resolver MAGE untuk pembuatan deskripsi kolom dan business title, serta implementasi versi terakhir (v6) ke `mage_flow`.

## Ringkasan

| Versi | Perubahan | Desc similar (dari 196) | Title similar |
|---|---|---|---|
| v3 | Cascade 7 resolver, `kata_similar` di urutan ke-2 | 167 (85,2%) | 159 (81,1%) |
| v4 | + konteks `kata_similar` diperkaya (saran #2) + kamus AS400 | 177 (90,3%) | 175 (89,3%) |
| v5 | + filter tipe data sebelum memilih (saran #3) | 177 (90,3%) | 177 (90,3%) |
| **v6** | + kamus `ao`/`chk`/`nik` + contoh few-shot dari steward (saran #4 setelah v5) | **181 (92,3%)** | 174 (88,8%) |

Reranker lintas resolver (saran #1) diuji di v4, v5, dan v6, tetapi hasilnya selalu lebih buruk dan tidak dipakai. v6 sudah diimplementasikan sebagai chain default di `mage_flow` (bagian 12).

| | Skrip | Hasil |
|---|---|---|
| v3 | `experiments/hierarchy_v3_eval.py` | `layer_eval_results_v3.xlsx` |
| v4 | `experiments/hierarchy_v4_eval.py` (+ `hierarchy_v4_patch_rows.py` untuk perbaikan token) | `layer_eval_results_v4_20261010_204502.xlsx` |
| v5 | `experiments/hierarchy_v4_patch_rows.py --type-filter` | `layer_eval_results_v5_typefilter.xlsx` |
| v6 | `experiments/hierarchy_v4_patch_rows.py --type-filter --approved-examples ... --all-rows` | `layer_eval_results_v6_domainknowledge.xlsx` |

Semua versi memakai data uji `data/test set eval v2 mage.xlsx` (196 kolom, 20 tabel) dan judge GPT-4.1 (similar / partial / unsimilar).

---

## 1. Metode v3

### 1.1 Hierarki (cascade)

v3 memakai **7 resolver yang disusun berurutan**. Untuk setiap kolom, jawaban pertama yang tersedia langsung dipakai (*first-hit-wins*).

```
kolom ─► 1. exact_match ─► 2. kata_similar ─► 3. kata_alias ─► 4. bm25_informatica
          ─► 5. bm25_confluence ─► 6. bm25_as400 (khusus tabel AS400) ─► 7. llm
```

| # | Resolver | Cara kerja singkat |
|---|---|---|
| 1 | exact_match | Lookup persis tabel + kolom di BM25 (prioritas AS400) |
| 2 | kata_similar | Ambil data element dari dataset KATA milik tabel, lalu LLM memilih yang cocok. Kalau semua ditolak, lakukan *rescue* (cari global dengan keyword hasil LLM) |
| 3 | kata_alias | Alias KATA yang sama persis dengan nama kolom / relasi teknis |
| 4 | bm25_informatica | Cari dokumentasi Informatica (certified), lalu LLM menyintesis deskripsi |
| 5 | bm25_confluence | Sama seperti no. 4, dengan sumber Confluence yang sudah diindeks BM25 |
| 6 | bm25_as400 | Sama seperti no. 4, dengan sumber kamus AS400 |
| 7 | llm | LLM menebak tanpa evidence (pilihan terakhir) |

### 1.2 Cara mengukur

- **Setiap resolver dijalankan terpisah pada semua 196 baris**, sehingga akurasi masing-masing bisa diukur sendiri.
- **Cascade diturunkan dari hasil terpisah itu**, dengan memilih resolver pertama yang menjawab untuk tiap baris. Hasilnya identik dengan menjalankan cascade sungguhan.
- **Judge GPT-4.1** membandingkan prediksi dengan ground truth (GT) untuk deskripsi dan business title.
- **Truthfulness:** auditor GPT-4.1 terpisah memeriksa apakah pilihan `kata_similar` didukung oleh evidence.

### 1.3 Input LLM pemilih di `kata_similar` (v3)

```
nama tabel + nama kolom + tipe data (sebagai "sinyal ringan")
+ evidence BM25 (khusus tabel AS400) + daftar kandidat KATA
```

## 2. Hasil v3

### 2.1 Akurasi per resolver (dijalankan sendiri)

| Resolver | Menjawab | Desc similar % | Title similar % |
|---|---|---|---|
| kata_similar | 182 | 88,4% | 84,0% |
| kata_alias | 37 | 81,1% | 70,3% |
| bm25_informatica | 180 | 63,1% | 47,4% |
| bm25_confluence | 105 | 46,2% | 37,3% |
| llm | 195 | 33,9% | 36,1% |
| bm25_as400 | 33 | 31,3% | 45,5% |
| exact_match | 0 | – | – |

### 2.2 Hasil cascade

| Langkah | Baris yang dijawab | Desc similar |
|---|---|---|
| kata_similar | 182 | 160 |
| bm25_informatica | 10 | 7 |
| llm | 4 | 0 |
| **Total** | **196** | **167 (85,2%)** |

Title similar: **159 (81,1%)**.

## 3. Kekurangan v3 (dari sheet Error Analysis)

Ada **39 baris** yang deskripsi atau title-nya tidak *similar*. Kategorinya:

| Kategori | Baris | Penjelasan |
|---|---|---|
| kata_similar: istilah terlalu generik | 10 | `acctbranch` → "Kode Kantor Cabang" (konteks *pembukaan rekening* hilang) |
| kata_similar: salah tafsir kata kunci | 9 | `cardsc` dibaca "card" → "Bin Kartu" (seharusnya Deskripsi Kolektibilitas) |
| GT title ambigu (desc sudah benar) | 9 | "Jenis Periode" vs "Periode Data" |
| AS400: kolom kriptik tanpa kamus | 7 | `jfxmsp` → "Saldo Valas" (seharusnya Max/Min Spread Kurs) |
| kata_similar: abaikan tipe data | 2 | `address_main` (int) → "Alamat Pembeli" (seharusnya Flag Alamat Utama) |
| Judge error | 2 | Prediksi identik dengan GT, tapi judge mengembalikan error |

**Akar masalah utama:** LLM pemilih di `kata_similar` hanya melihat nama tabel dan nama kolom. Nama kolom sering berupa singkatan atau terlalu umum, sehingga LLM terpaksa menebak. Tiga kategori `kata_similar` (21 baris) berasal dari masalah ini.

Kekurangan lain:
- **Kamus singkatan AS400 belum lengkap**, misalnya CAR, SPR, MSP, dan FCS belum ada.
- **First-hit-wins tidak bisa mengoreksi jawaban yang salah.** Ada 12 baris yang deskripsinya salah, padahal resolver lain menjawab benar.

---

## 4. Metode v4

v4 = v3 + dua perbaikan dengan prioritas tertinggi dari Error Analysis. Resolver lain tidak diubah.

### 4.0 Persiapan: kamus singkatan AS400 (saran #4 di sheet Error Analysis)

Sebelum eksperimen, kamus singkatan di layanan BM25 (`PUT /api/v1/terms`, `localhost:8003`) ditambah term berikut:

| Term | Arti |
|---|---|
| car | collectibility (kolektibilitas) |
| dsc | description |
| spr | buy/sell spread |
| msp | maximum and minimum spread |
| fcs | floor ceiling spread |
| dcd | currency code |
| drn | exchange rate id (rate number) |
| card | card (kartu), penyeimbang agar `car` tidak menimpa arti kolom kartu |

### 4.1 Saran #2: `kata_similar` dengan konteks diperkaya

Diaktifkan lewat `KataSimilarResolver(enriched_context=True)` di `mage_flow/kata_similar.py`.

**Ide utamanya:** kandidat tetap diambil dari KATA (sumber terkurasi). Bedanya, LLM pemilih kini menerima lebih banyak petunjuk dan aturan memilih yang lebih ketat.

| Input ke LLM pemilih | v3 | v4 |
|---|:-:|:-:|
| Nama tabel, nama kolom, evidence AS400 | ✔ | ✔ |
| Tipe data | ✔ (sinyal ringan) | ✔ + aturan kompatibilitas |
| System context | ✘ | ✔ |
| Istilah tabel | ✘ | ✔ |
| Ekspansi singkatan kolom (`/terms/context`) | ✘ | ✔ |
| Kolom tetangga + tipe datanya | ✘ | ✔ |
| Output confidence | ✘ | ✔ |

Aturan memilih yang ditambahkan:
1. **Tentukan domain tabel** dari nama, system context, istilah tabel, dan kolom tetangga.
2. **Setiap bagian nama kolom harus terjelaskan** (`acctbranch` = account + branch).
3. **Nama generik** (`id`, `name`, `code`, `status`, `amount`, …) artinya diambil dari entitas tabel.
4. **Tipe data harus cocok**: kolom int bukan teks bebas, string JSON bukan flag, `updated_at` bukan tanggal ingestion.
5. **Pilih yang paling spesifik.**
6. **Tolak semua kandidat** jika tidak ada yang benar-benar cocok.

Satu perubahan lagi di jalur *rescue*: untuk kolom bernama generik, query pencarian ditambah nama dataset/tabel. Contoh: "kode" menjadi "kode kodepos".

Alur per kolom:

```
kolom ─► ① kumpulkan petunjuk (singkatan, tetangga, tipe data, system context)
      ─► ② ambil kandidat dari dataset KATA milik tabel
      ─► ③ LLM memilih dengan aturan di atas
           ├─ terpilih ─► definisi KATA = deskripsi, nama element = business title
           └─ ditolak  ─► rescue (query + nama tabel untuk kolom generik) ─► pilih lagi
                          └─ tetap ditolak ─► lanjut ke kata_alias (cascade)
```

Contoh, kolom `cardsc` di tabel `0000_staging_raw_as4_lnparr`:

```
v3:  "cardsc" ─► dibaca "card" ─► Bin Kartu                         ✘
v4:  singkatan : car = kolektibilitas, dsc = deskripsi
     tetangga  : carcod, cartyp (pasangan kode/deskripsi)
     domain    : tabel pinjaman
     ─► Deskripsi Kolektibilitas                                    ✔
```

### 4.2 Perbaikan batas token (setelah run pertama)

Pada run pertama v4, 4 baris gagal di `kata_similar`. Penyebabnya, jawaban LLM terpotong karena batas token terlalu kecil untuk prompt yang lebih panjang:
- 3 baris gagal parse JSON (`periodelaporan` ×2, `userid_ao`),
- 1 baris mendapat keyword kosong (`ddycod`).

Batas token dinaikkan:

| Konstanta | Sebelum | Sesudah |
|---|---|---|
| `SELECTION_MAX_TOKENS` | 900 | 2000 |
| `KEYWORD_MAX_TOKENS` | 150 | 400 |

Keempat baris itu lalu dijalankan ulang dengan `experiments/hierarchy_v4_patch_rows.py`. Hanya `kata_similar` untuk 4 baris tersebut yang dijalankan dan dinilai ulang. Semua baris lain tetap memakai label judge dari run pertama, sehingga tidak ada variasi akibat penilaian ulang.

### 4.3 Saran #1: reranker lintas resolver

Langkah ini menggantikan *first-hit-wins*. Ketujuh resolver tetap dijalankan, lalu LLM generator memilih satu jawaban dari semua deskripsi berbeda yang dihasilkan. Bahan pertimbangannya:
- evidence tiap jawaban,
- confidence `kata_similar`,
- hasil truthfulness, dipakai sebagai sinyal saja (bukan syarat).

Pilihan reranker otomatis mewarisi label judge dari resolver yang dipilih, sehingga perbandingan dengan cascade bersifat eksak.

## 5. Hasil v4

### 5.1 Ringkasan

| Konfigurasi | Desc similar | Title similar |
|---|---|---|
| v3 cascade (baseline) | 167 (85,2%) | 159 (81,1%) |
| **v4: #2, tetap first-hit-wins (final)** | **177 (90,3%)** | **175 (89,3%)** |
| v4: #2 sebelum perbaikan batas token | 178 (90,8%) | 175 (89,3%) |
| v4: #1 + #2 (reranker) | 153 (78,1%) | 145 (74,0%) |
| v4: reranker hanya aktif saat confidence rendah (simulasi, sebelum perbaikan token) | maks. 177 | maks. 176 |
| Batas atas (pilihan resolver ideal per baris) | 184 (93,9%) | – |

### 5.2 `kata_similar` dijalankan sendiri

| | v3 | v4 |
|---|---|---|
| Baris yang dijawab | 182 | 190 |
| Desc similar % | 88,4% | **92,1%** |
| Desc similar (dari 196) | 160 | **174** |
| Title similar % | 84,0% | **91,6%** |
| Truthful % | 91,8% | 87,2% |

### 5.3 Perubahan per baris (v3 → v4 cascade)

**21 baris membaik dan 10 memburuk (selisih +10).** Baris yang membaik hampir persis sama dengan baris yang masuk Error Analysis v3:

| Kolom | v3 | v4 | Yang membantu |
|---|---|---|---|
| `cardsc` | Bin Kartu ✘ | Deskripsi Kolektibilitas ✔ | singkatan + tetangga |
| `carcod` | ✘ | Kolektibilitas ✔ | kamus singkatan |
| `tramt` | Jumlah Blokir ✘ | Jumlah Transaksi ✔ | semua token terjelaskan |
| `acctbranch` | Kode Kantor Cabang ~ | Kode Kantor Cabang Pembukaan Rekening ✔ | spesifisitas |
| `address_main` | Alamat Pembeli ✘ | Flag Alamat Utama ✔ | tipe data |
| `updated_at` | Tanggal Ingestion ~ | Perubahan Data ✔ | tipe data / makna nama |
| `business` | FBI Bancassurance ✘ | Jenis New Business ✔ | nama generik → entitas tabel |
| `jfxmsp`, `jfxspr`, `jfxfcs`, `jfxdcd` | ✘ / ~ | ✔ | kamus AS400 + konteks |

### 5.4 Efek perbaikan batas token

Bug-nya hilang (tidak ada lagi error parse), tetapi total deskripsi benar justru turun 1 baris, dari 178 menjadi 177.

| Kolom | GT | Sebelum perbaikan | Sesudah perbaikan |
|---|---|---|---|
| `periodelaporan` (_d_) | Jenis Periode | gagal → bm25_informatica: partial | kata_similar "Periode Bulan": partial |
| `periodelaporan` (_m_) | Jenis Periode | gagal → bm25_informatica: **similar** | kata_similar "Periode Report": **partial** |
| `userid_ao` | PN RM | gagal → rescue "User ID": partial | langsung "User ID": partial |
| `ddycod` (ddyp2a) | Kode Produk Mata Uang | keyword kosong → bm25_informatica: partial | keyword berhasil, semua kandidat ditolak → bm25_informatica: partial |

Penurunan ini datang dari `periodelaporan`, kasus GT ambigu ("Jenis Periode") yang sudah tercatat di Error Analysis v3. Begitu `kata_similar` berhasil menjawab, jawabannya menutupi jawaban bm25_informatica yang kebetulan dinilai *similar* di satu tabel. Jadi penurunannya tidak berarti perbaikan token memperburuk pemilihan.

### 5.5 Kenapa reranker (#1) gagal

- **Reranker mengganti 45 jawaban cascade**, dan 28 di antaranya sebelumnya sudah *similar* tapi malah turun. Penyebabnya, reranker cenderung memilih deskripsi sintesis BM25/LLM yang *terdengar* lebih spesifik daripada definisi KATA yang sebenarnya sudah benar.
- **12 baris gagal di reranker** (output LLM tidak valid) lalu kembali ke jawaban cascade.
- **Ruang perbaikannya kecil.** Setelah #2, `kata_similar` sudah 92% tepat, sehingga selisih ke batas atas (184) tinggal 7 baris. Reranker berbasis LLM belum cukup andal untuk mengambil 7 baris itu tanpa merusak yang lain.

**Kesimpulan:** pakai #2 dengan first-hit-wins. Reranker tidak direkomendasikan.

## 6. Kekurangan v4

1. **10 baris memburuk dibanding v3.**
   - 4 baris `periodelaporan` (tabel _d_, _m_, _q_, _w_): `kata_similar` memilih "Periode Data/Bulan/Report", sedangkan GT title-nya "Jenis Periode". Ini lebih tepat disebut masalah GT yang ambigu (saran #5) daripada masalah pipeline.
   - 4 baris salah pilih kandidat: `total` → "Jumlah Transaksi" (GT: Total Amount Transaksi), `last_name` → "Nama Debitur" (GT: Nama Belakang), `ddydat` → "Hari Transaksi" (GT: Tanggal Maintenance), dan `id` → "ID Data" (GT: Id Transaksi).
   - 2 baris (`chknum`, `nik`): `kata_similar` menolak semua kandidat, padahal di v3 kandidat yang benar terpilih. Jawabannya jatuh ke bm25_informatica yang hasilnya lebih buruk. Aturan baru yang lebih ketat membuat LLM pemilih lebih sering menolak.
   - Batas token yang terlalu kecil (penyebab 4 baris gagal di run pertama) sudah diperbaiki (lihat 4.2).
2. **Kolom tetangga diambil dari test set** (~10 kolom per tabel), bukan dari skema tabel lengkap. Di produksi konteksnya bisa lebih kaya, tapi hasilnya belum diukur.
3. **Istilah tabel selalu kosong**, karena endpoint `/api/v1/tables/term-context` mengembalikan 404 di service `localhost:8003`.
4. **Perbandingan v3 → v4 tidak murni efek #2.**
   - Kamus singkatan AS400 (#4) ikut ditambahkan, sehingga baris AS400 terbantu oleh keduanya.
   - Hasil resolver yang kodenya tidak diubah ikut bergeser. Contohnya, bm25_confluence menjawab 105 baris di v3 tapi 184 baris di v4. Kemungkinan karena indeks atau layanan BM25 berbeda antar-run.
   - Meski begitu, peningkatan terbesar terjadi di `kata_similar`, yang memang menjawab 190 dari 196 baris.
5. **Kamus singkatan menghasilkan arti yang bertabrakan.** Endpoint mengembalikan semua potongan nama yang cocok, bukan hanya yang terpanjang. Contohnya, `jfxdrn` tetap juga mendapat `dr` = debit, dan kolom `card_*` di tabel non-AS400 ikut mendapat arti "collectibility".
6. **Truthful % turun** dari 91,8% ke 87,0%. Pilihan yang memakai penalaran kontekstual (misalnya menebak arti dari kolom tetangga) lebih sering dinilai "tidak didukung evidence secara eksplisit" oleh auditor.
7. **Belum masuk ke pipeline produksi.** Sudah dikerjakan untuk v6 (bagian 12).
8. **Masalah di luar #2 belum ditangani:** GT title yang ambigu (9 baris, saran #5) dan judge error (saran #6) belum diperbaiki.

---

## 7. Metode v5: filter tipe data (saran #3)

### 7.1 Masalah yang ingin diatasi

Di v3, 2 baris salah karena `kata_similar` mengabaikan tipe data, misalnya kolom `int` dipetakan ke elemen teks "Alamat Pembeli". Di v4, aturan tipe data masih berupa instruksi di prompt. Saran #3 meminta aturan yang diterapkan secara pasti **sebelum** LLM memilih.

### 7.2 Cara kerja

Diaktifkan lewat `KataSimilarResolver(type_filter=True)`, dengan fungsi `type_conflict()` / `filter_by_data_type()` di `mage_flow/kata_similar.py`.

Elemen KATA tidak menyimpan tipe data (`format_type` selalu kosong), jadi jenis nilai kandidat disimpulkan dari **awal namanya**:

| Tipe data kolom | Kandidat yang dibuang |
|---|---|
| int, bigint, tinyint, decimal, double, boolean | teks bebas: diawali Nama, Alamat, Deskripsi, Keterangan, Catatan, Uraian, Email, Judul |
| timestamp, date | yang jelas bukan waktu: teks bebas, atau diawali Jumlah, Nominal, Saldo, Total, Nilai, Kode, Nomor, ID, Flag, Status, Persentase |

```
kandidat KATA ─► filter tipe data ─► kandidat yang lolos ─► LLM memilih (prompt v4)
                 (deterministik,      (pool dataset dan
                  tanpa LLM)           pool rescue)
```

Aturannya sengaja dibuat longgar: tanggal yang disimpan sebagai angka (AS400 decimal YYYYMMDD) tetap diizinkan. Aturan ini sudah dicek terhadap test set: **tidak ada satu pun business title GT yang ikut tertolak**.

### 7.3 Cara mengukur

Filter ini deterministik dan hanya berpengaruh pada kolom numerik atau timestamp. Jadi `kata_similar` hanya dijalankan ulang pada **42 baris** yang memang terdampak: pool kandidatnya berisi elemen yang tidak cocok tipe, atau di v4 baris itu masuk ke jalur rescue. Baris lain mendapat input yang sama persis dengan v4, sehingga hasilnya diambil dari v4.

## 8. Hasil v5

| | v4 | v5 |
|---|---|---|
| Desc similar | 177 | 177 |
| Title similar | 175 | **177** |
| `kata_similar` sendiri: desc similar | 174 | 175 |

40 dari 42 baris memilih istilah yang sama persis dengan v4. Hanya 2 yang berubah, dan keduanya lewat jalur rescue:

| Kolom | GT | v4 | v5 | Desc | Title |
|---|---|---|---|---|---|
| `brimw_summary_kliring.id` | Id Transaksi | ID Data | Id Transaksi | partial → similar | partial → similar |
| `cms_clientabonementtrx.status` | Status Transaksi | (bm25) | Status Transaksi CS | similar → partial | partial → similar |

**Kenapa hampir tidak berpengaruh:** aturan tipe data di prompt v4 sudah bekerja. Dari semua pilihan `kata_similar` di v4, **tidak ada satu pun yang melanggar aturan tipe data**, jadi tidak ada yang perlu dikoreksi filter.

## 9. Kekurangan v5

1. **Tidak ada kenaikan akurasi deskripsi.** Manfaatnya lebih ke pengaman: hasilnya pasti dan pool kandidat jadi lebih kecil.
2. **Perubahan 2 baris tidak bisa dipastikan berasal dari filter.** Bisa juga karena variasi acak LLM (temperature 0,1).
3. **Kasus JSON tidak terdeteksi.** Kolom `override` (string berisi JSON, GT "Informasi JSON Override Pinjaman BRISPOT") tetap bertipe "string". Deteksinya butuh sampel nilai kolom.
4. **Jenis nilai disimpulkan dari nama kandidat**, jadi bisa salah untuk nama yang tidak lazim.

---

## 10. Metode v6: pengetahuan domain (saran #4 dari daftar saran setelah v5)

### 10.1 Masalah yang ingin diatasi

Sisa error di v5 sebagian besar adalah kolom yang artinya tidak bisa ditebak dari nama saja, misalnya `userid_ao` (GT: PN RM), `nik`, `chknum`, dan `jfxdrn`. LLM perlu pengetahuan tentang singkatan dan kebiasaan penamaan di BRI.

### 10.2 Tambahan kamus singkatan

Ditambahkan lewat `PUT /api/v1/terms` di `localhost:8003`:

| Term | Arti |
|---|---|
| ao | account officer (AO), also called relationship manager (RM) |
| chk | cheque (cek) |
| nik | nomor induk kependudukan / nomor identitas (NIK) |

Singkatan `ddy` tidak ditambahkan karena artinya belum diketahui pasti.

### 10.3 Contoh few-shot dari metadata yang sudah divalidasi steward

Modul baru `mage_flow/approved_examples.py`, dengan sumber data `MAGE - Human Evaluation for MAGE.xlsx` sheet *Validate*:
- **10.476 kolom dari 100 tabel** yang sudah direview steward.
- Nama dan definisi yang dipakai adalah referensi steward (MMT). Kalau MMT kosong, dipakai output MAGE yang dinilai *Pass* atau *Similar*.
- **Tidak ada tabel maupun kolom yang tumpang-tindih dengan test set**, jadi evaluasinya tidak bocor. Saat eksperimen, semua tabel test set juga dikecualikan secara eksplisit.

Untuk setiap kolom, diambil maksimal 5 kolom bernama mirip dari tabel lain:
- nama kolomnya sama persis, atau
- kemiripan trigram karakternya ≥ 0,34, atau
- khusus AS400: akhiran 3 hurufnya sama (misalnya `...dat`, `...cod`).

Contoh ini dimasukkan ke prompt `kata_similar` sebagai referensi kebiasaan penamaan, **bukan sebagai kandidat**. Pilihan akhir tetap harus berasal dari kandidat KATA dan cocok dengan tabelnya.

Contoh hasil pencarian:

```
userid_ao ─► brispot_history_mst_prakarsa.userid_ao => "PN Mantri"
ddydat    ─► cdtnew.lpdat => "Last Pay Date", cdtnew.npdat => "Next Pay Date"
```

### 10.4 Cara mengukur

Contoh few-shot berpengaruh ke setiap kolom, jadi `kata_similar` dijalankan ulang untuk **semua 196 baris**, dengan filter tipe data v5 tetap aktif. Resolver lain memakai hasil v5.

## 11. Hasil dan kekurangan v6

### 11.1 Hasil

| | v5 | v6 |
|---|---|---|
| Desc similar | 177 (90,3%) | **181 (92,3%)** |
| Title similar | 177 (90,3%) | 174 (88,8%) |
| `kata_similar` sendiri: baris yang dijawab | 191 | 190 |
| `kata_similar` sendiri: desc similar % | 92,1% | 93,7% |
| `kata_similar` sendiri: truthful % | 87,3% | 86,8% |
| Reranker (pembanding) | 154 | 147 |
| Batas atas (pilihan resolver ideal) | 185 | 183 |

7 baris membaik dan 4 memburuk:

| Kolom | GT | v5 | v6 | Desc |
|---|---|---|---|---|
| `userid_ao` | PN RM | User ID | **PN RM** | partial → similar ✔ |
| `nik` | Nomor ID | (bm25) | **Nomor ID** | partial → similar ✔ |
| `jfxdrn` | ID Kurs | Kurs | **ID Kurs** | partial → similar ✔ |
| `periodelaporan` ×3 | Jenis Periode | Periode Bulan/Report | Periode Data | partial → similar ✔ |
| `status` | Status Transaksi | Status Transaksi CS | (bm25) | partial → similar ✔ |
| `ddprdc` | Kode Produksi Referensi | ✔ | Kode Produk Mata Uang | similar → partial ✘ |
| `cartyp` | Tipe Klasifikasi Paramater Loan | ✔ | Kolektibilitas | similar → partial ✘ |
| `id` (kliring) | Id Transaksi | ✔ | ID Data | similar → partial ✘ |
| `branch` | Kode Unit Kerja | ✔ | Nomor Referensi Cabang | similar → unsimilar ✘ |

`userid_ao` terbantu oleh dua hal sekaligus: term `ao` yang baru, dan contoh "PN Mantri" dari tabel BRISPOT lain.

### 11.2 Kekurangan

1. **Business title turun 3 baris** (177 → 174), meski deskripsi naik 4 baris.
2. **Sebagian perubahan kemungkinan hanya variasi acak.** Karena `kata_similar` dijalankan dan dinilai ulang untuk semua baris, sebagian selisih bisa berasal dari variasi LLM atau judge. Contohnya `id` (kliring) yang bolak-balik antara "ID Data" dan "Id Transaksi" di setiap run.
3. **Efek samping kamus `car`:** `cartyp` kini ikut ditafsirkan sebagai "Kolektibilitas". Ini contoh langsung masalah tabrakan arti singkatan (bagian 6, nomor 5).
4. **Term baru dipilih dari baris yang salah di test set**, jadi sedikit bias ke test set, sama seperti term AS400 di v4.
5. **Data validasi steward berisi beberapa entri kotor**, misalnya `total_value` → "Kecamatan". LLM hanya memakainya sebagai referensi, tapi tetap bisa terpengaruh.
6. **Technical relation KATA tidak ditambahkan.** Itu berarti menulis ke cache KATA bersama (`bribrain_mage_kata_technical_relations_cache`), dan kalau isinya diambil dari test set, evaluasinya bocor.
7. **`chknum` tetap salah di semua versi.** Tidak ada satu pun resolver yang menjawab benar ("Nomor Cek").

---

## 12. Implementasi v6 ke `mage_flow`

### 12.1 Chain

`settings["resolver_chain"]` memilih chain, dengan default `"v6"`:

```
exact_as400 ─► kata_similar ─► kata_evidence ─► bm25_informatica_certified
            ─► bm25_confluence ─► bm25_as400 ─► pure_llm
```

Chain lama tetap tersedia sebagai `"legacy"` untuk rollback:

```
exact_as400 ─► bm25_as400 ─► kata_evidence ─► bm25_confluence
            ─► bm25_informatica_certified ─► confluence_fallback ─► pure_llm
```

### 12.2 Perubahan kode

| File | Perubahan |
|---|---|
| `config.py` | `settings["resolver_chain"]`, blok `settings["kata_similar"]` (enabled, enriched_context, type_filter, use_bm25_context, enable_rescue, approved_examples), dan env `MAGE_APPROVED_EXAMPLES_PATH` |
| `resolvers.py` | `build_resolver_chain(settings)` memilih `build_v6_resolver_chain()` atau `build_legacy_resolver_chain()`. `ResolverContext` mendapat field `data_type`, `extra_knowledge`, `table_columns` |
| `flow.py` | Chain dipilih sesuai settings dan di-cache, jadi pool KATA dan file contoh dimuat sekali saja. Tipe data dan daftar kolom tabel diteruskan ke resolver. Discovery Confluence dilewati kalau chain tidak memakai Confluence. `generate_column` mendapat parameter `data_type` |
| `kata_similar.py` | Opsi `enriched_context`, `type_filter`, `examples_provider`, dan `evidence_resolver` (mengambil sendiri evidence BM25 untuk tabel AS400). Bisa dimatikan lewat `settings["kata_similar"]["enabled"]`. `SELECTION_MAX_TOKENS` = 2000, `KEYWORD_MAX_TOKENS` = 400 |
| `approved_examples.py` | Modul baru untuk contoh few-shot |
| `__init__.py`, `docs/README_mage_flow.md` | Dokumentasi chain v6 dan legacy |

### 12.3 Uji end-to-end

`generate_metadata` dijalankan pada 3 tabel test set (`10000_cr_bl008`, `brispot_ref_flag_overide_sme`, `0000_staging_raw_as4_jhfxdt`; total 31 kolom) dengan business title aktif. Hasilnya:
- tidak ada kolom yang gagal,
- 30 dari 31 business title sesuai GT,
- hasilnya sama dengan eksperimen v6. Satu-satunya yang salah, `chknum`, juga salah di eksperimen.

Dua kolom yang di eksperimen salah (`branch`, `override`) di uji ini kebetulan benar. Kemungkinan besar itu variasi acak LLM, bukan perbaikan.

### 12.4 Yang perlu diperhatikan sebelum produksi

1. **Kirim `ColumnDataType` untuk setiap kolom.** Filter tipe data dan konteks kolom tetangga bergantung padanya. Tanpa itu, keduanya tidak berfungsi tapi juga tidak error.
2. **File contoh few-shot belum masuk git.** Secara default dibaca dari `MAGE - Human Evaluation for MAGE.xlsx` di root proyek, atau dari `MAGE_APPROVED_EXAMPLES_PATH`. Kalau file tidak ada, `kata_similar` tetap jalan tanpa contoh dan mencatat warning.
3. **Discovery Confluence tidak lagi dijalankan** di chain v6.
4. **Biaya per kolom naik sedikit:** ada satu panggilan BM25 tambahan untuk ekspansi singkatan, dan pencarian evidence AS400 bisa terjadi dua kali di tabel AS400.
5. **Endpoint `/api/v1/tables/term-context` mengembalikan 404** di `localhost:8003`, sehingga istilah tabel selalu kosong.

## 13. Langkah berikutnya

1. ~~Naikkan `SELECTION_MAX_TOKENS` dan jalankan ulang baris yang gagal parse.~~ Sudah dikerjakan (4.2).
2. ~~Masukkan `KataSimilarResolver` ke `build_resolver_chain()` dan isi `data_type` serta `table_columns` dari `flow.process_column`.~~ Sudah dikerjakan (bagian 12).
3. **Perbaiki evaluasinya** (saran #5 dan #6): standarkan GT title, longgarkan judge title untuk sinonim, dan ulangi judge kalau labelnya tidak valid.
4. **Tambahkan sampel nilai kolom** ke prompt dan filter tipe data. Ini bisa memecahkan `override` (JSON), `ddydat` (tanggal Julian), dan `code` (kode kelurahan), serta memastikan arti `periodelaporan`.
5. **Pilih istilah untuk satu tabel sekaligus**, supaya pasangan kolom seperti `amount` dan `total` tidak tertukar.
6. **Perbaiki lookup singkatan di layanan BM25** agar mengutamakan potongan terpanjang, atau batasi kamus AS400 ke tabel AS400 saja. Ini mengatasi efek samping `car` pada `cartyp`.
7. **Ukur variasi acak:** jalankan v6 beberapa kali untuk melihat seberapa besar selisih antar-run, supaya perbedaan kecil (±3 baris) bisa dibaca dengan benar.
