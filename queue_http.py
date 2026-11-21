"""Opt-in transport adapters; reuse accepted intake, analysis and rendering."""
import io
import json

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from document_intake import (quote_groups, upload_form, validate_customer,
                             validate_filename, validate_text, validate_total_text)
from durable_uploads import UploadSource
from purchase_security import QUOTE_LIMITS
from report_jobs import PRIVATE_HEADERS, public_status, request_key


def _key(request):
    if 'idempotency-key' not in request.headers:
        raise HTTPException(400, 'Durable submissions require an Idempotency-Key; retain it to recover a lost acknowledgment.')
    if request.headers.get('x-review-retry', 'false') != 'false':
        raise HTTPException(409, 'The durable queue retries safe preparation automatically. Check saved status; uncertain work requires support.')
    return request_key(request)


def _ack(runtime, authorization, row):
    # Replays must not acknowledge an old missing/corrupted input reference.
    runtime.storage.load(authorization, row['job_id'])
    row = runtime.records.get(row['job_id'], authorization.purchase)
    runtime.wake()
    return JSONResponse(public_status(row), status_code=202, headers={**PRIVATE_HEADERS,
        'X-Review-Job-ID': row['job_id'], 'Location': f"/jobs/{row['job_id']}", 'Retry-After': '3'})


async def enqueue_upload(runtime, request, authorization):
    runtime.require_accepting(authorization)
    key = _key(request)
    async with upload_form(request, QUOTE_LIMITS[authorization.tier]) as form:
        files = form.getlist('files')
        groups = quote_groups(form, QUOTE_LIMITS[authorization.tier])
        authorization.validate_submission(len(groups), form.get('package'), form.get('packageName'))
        name, email = validate_customer(form.get('customer_name'), form.get('customer_email'))
        sources = [UploadSource(file.filename, file.content_type, file.file) for file in files]
        # Await completion before parser cleanup; an HTTP disconnect never owns
        # the lifetime of an accepted job. A lost response is recovered by key.
        row = await run_in_threadpool(runtime.storage.save, authorization, key, sources, groups,
                                     customer_name=name, customer_email=email)
    return await run_in_threadpool(_ack, runtime, authorization, row)


async def enqueue_analysis(runtime, request, http_request, authorization):
    runtime.require_accepting(authorization)
    key = _key(http_request)
    authorization.validate_submission(len(request.files), request.package, request.packageName)
    name, email = validate_customer(request.customerName, request.customerEmail,
                                    request.customer_name, request.customer_email)
    sources, names, texts = [], [], []
    for index, file in enumerate(request.files, 1):
        if file.downloadUrl or file.fileUrl:
            raise HTTPException(400, 'File URLs are not supported. Submit quote text or upload documents.')
        if file.fileName is not None and file.originalFileName is not None and file.fileName != file.originalFileName:
            raise HTTPException(400, 'Conflicting quote file names.')
        filename = file.fileName if file.fileName is not None else file.originalFileName
        filename = filename if filename is not None else f'quote{index}.txt'
        validate_filename(filename)
        text = validate_text(file.extractedText)
        texts.append(text)
        names.append(filename)
        sources.append(UploadSource(f'quote-{index}.txt', 'text/plain', io.BytesIO(text.encode('utf-8'))))
    validate_total_text(texts)
    metadata = {field: getattr(request, field) for field in
                ('contractor_1_name', 'contractor_2_name', 'contractor_3_name', 'city', 'state', 'project_zip')}
    metadata['file_names'] = names
    groups = [{'pages': [{'file': index, 'rotation': 0}]} for index in range(len(sources))]
    row = await run_in_threadpool(runtime.storage.save, authorization, key, sources, groups,
        customer_name=name, customer_email=email, analysis_metadata=metadata)
    return await run_in_threadpool(_ack, runtime, authorization, row)


async def process_saved_submission(manifest, files, grant):
    # Lazy import avoids coupling the storage/scheduler modules to main's model
    # initialization. These are the SAME accepted functions used by compatibility mode.
    import main
    metadata = manifest.get('analysis_metadata')
    if metadata is not None:
        quotes = [main.UploadedQuote(fileName=metadata['file_names'][index],
            originalFileName=metadata['file_names'][index], extractedText=files[group[0]['file']].decode('utf-8'))
            for index, group in enumerate(manifest['quotes'])]
        fields = {key: value for key, value in metadata.items() if key != 'file_names'}
        request = main.AnalyzeRequest(package=grant.tier, customerName=manifest['customer_name'],
            customerEmail=manifest['customer_email'], files=quotes, **fields)
        await main.analyze_hvac_quote(request, grant)
        return
    attachments, groups = [], []
    for group in manifest['quotes']:
        pages = []
        for item in group:
            pages.append({'file': len(attachments), 'rotation': item['rotation']})
            attachments.append(('files', (item['filename'], files[item['file']], item['content_type'])))
        groups.append({'pages': pages})
    # HTTPX only encodes a local multipart body; no client/send or network call.
    encoded = httpx.Request('POST', 'http://local.invalid/upload', data={
        'customer_name': manifest['customer_name'], 'customer_email': manifest['customer_email'],
        'quote_groups': json.dumps(groups), 'package': grant.tier}, files=attachments)
    body = encoded.read()
    sent = False
    async def receive():
        nonlocal sent
        if sent:
            return {'type': 'http.disconnect'}
        sent = True
        return {'type': 'http.request', 'body': body, 'more_body': False}
    request = Request({'type': 'http', 'method': 'POST', 'path': '/upload',
        'headers': [(b'content-type', encoded.headers['content-type'].encode())]}, receive)
    await main.upload_file(request, grant)
