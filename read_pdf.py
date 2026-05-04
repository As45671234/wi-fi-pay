import pdfplumber
with pdfplumber.open(r'доки/kaspi/Guide Pay with Kaspi.kz.pdf') as pdf:
    for i, page in enumerate(pdf.pages):
        print(f'=== PAGE {i+1} ===')
        print(page.extract_text())
