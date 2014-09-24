from flask import render_template, request
import traceback
import h5py
import numpy as np
from scipy.ndimage.filters import convolve1d
import codecs
import re
from itertools import starmap
from collections import Counter
from data import get_nwd, WordTuple, DocumentTuple, TopicTuple, get_docs_info, get_topics_info, get_words_info
from app import app
from whoosh import analysis, formats, index, qparser, query as wh_query, highlight, sorting


def error_handler(error):
    if hasattr(error, 'code'):
        params = {
            'code': error.code,
            'desc': error.description,
            'name': error.name,
        }
    else:
        error.code = 500
        params = {
            'code': error.code,
            'desc': error.message,
            'tech_desc': traceback.format_exc(),
            'name': error.__class__.__name__,
        }

    return render_template('error.html', **params), error.code


for error in range(400, 420) + range(500, 506):
    app.errorhandler(error)(error_handler)
app.errorhandler(Exception)(error_handler)


@app.route('/')
def overview():
    with h5py.File('../data.hdf', mode='r') as h5f:
        nws = get_nwd(h5f).sum(1).A1
        ws = nws.argsort()[::-1]
        words = h5f['dictionary'][...][ws]
        words = list( starmap(WordTuple, zip(ws, nws[ws], words)) )

        nds = get_nwd(h5f).sum(0).A1
        ds = nds.argsort()[::-1]
        meta = h5f['metadata'][...][ds]
        docs = list( starmap(DocumentTuple, zip(ds, nds[ds], meta)) )

        ptds = h5f['p_td'][...]
        pts = ptds.dot(1.0 * nds / nds.sum())
        ts = pts.argsort()[::-1]
        topics = list( starmap(TopicTuple, zip(ts, pts[ts])) )

    return render_template('overview.html', words=words, docs=docs, topics=topics)


@app.route('/search')
@app.route('/search/', defaults={'query': ''})
@app.route('/search/<query>')
def search(query=None):
    return render_template('document/search.html', query=query or '')

class RemoveDuplicatesFilter(analysis.Filter):
    def __call__(self, stream):
        lasttext = None
        for token in stream:
            if lasttext != token.text:
                yield token
            lasttext = token.text

class WithData(formats.Format):

    def word_values(self, value, analyzer, **kwargs):
        fb = self.field_boost

        for text, val in value:
            yield (text, 1, fb, formats.pack_float(val))

    def decode_data(self, valuestring):
        return formats.unpack_float(valuestring)[0]

    def decode_frequency(self, valuestring):
        return 1

    def decode_weight(self, v):
        return self.field_boost

@app.route('/search_results/')
@app.route('/search_results/<query>')
def search_results(query='*'):
    ix = index.open_dir('../whoosh_ix', readonly=True, indexname='docs')

    fields = ['title', 'authors', 'authors_ngrams', 'title_ngrams']
    if request.args.get('in_text', False) == 'true':
        fields = ['content'] + fields
    qp = qparser.MultifieldParser(fields,
                                  ix.schema,
                                  termclass=wh_query.FuzzyTerm)

    highlighter_whole = highlight.Highlighter(fragmenter=highlight.WholeFragmenter())
    def hl_whole(hit, field, text=None):
        return highlighter_whole.highlight_hit(hit, field, text)

    highlighter_content = highlight.Highlighter(
        fragmenter=highlight.PinpointFragmenter(surround=50, maxchars=1000, autotrim=True),
        formatter=highlight.HtmlFormatter(between='<hr/>')
    )
    def hl_content(hit):
        return highlighter_content.highlight_hit(hit, 'content')

    def htopics(hit):
        topics = [TopicTuple(name, ptd)
                  for name, ptd in searcher.vector_as('data', hit.docnum, 'topics')]
        topics.sort(key=lambda t: t.np, reverse=True)
        return topics

    with ix.searcher() as searcher:
        query_parsed = qp.parse(query)

        kwargs = {}
        if 'groupby[]' in request.args:
            kwargs['groupedby'] = sorting.MultiFacet(
                items=[sorting.FieldFacet(field, allow_overlap=True) if not field.endswith('_stored') else sorting.StoredFieldFacet(field[:-7])
                       for field in request.args.getlist('groupby[]')])

        results = searcher.search(query_parsed, limit=50, terms=True, **kwargs)

        if not results:
            corrected = searcher.correct_query(query_parsed, query)
            if corrected.string != query:
                corrected.html = corrected.format_string(highlight.HtmlFormatter())
            else:
                corrected = None
        else:
            corrected = None

        if results.facet_names():
            groups = sorted(results.groups().items(), key=lambda gr: (-len(gr[1]), gr[0]))
            grouped = [(' '.join(map(str, gr_name)) if isinstance(gr_name, tuple) else gr_name,
                        [next(hit for hit in results if hit.docnum == docnum)
                         for docnum in gr_nums])
                       for gr_name, gr_nums in groups]
            results_cnt = sum(len(gr) for _, gr in grouped)
        else:
            grouped = None
            results_cnt = len(results)

        return render_template('document/search_results.html',
                               query=query,
                               grouped=grouped,
                               results=results,
                               results_cnt=results_cnt,
                               hl_whole=hl_whole,
                               hl_content=hl_content,
                               htopics=htopics,
                               corrected=corrected)


@app.route('/topics')
def topics():
    with h5py.File('../data.hdf', mode='r') as h5f:
        ptds = h5f['p_td'][...]
        nds = get_nwd(h5f).sum(0).A1
        pts = ptds.dot(1.0 * nds / nds.sum())
        indices = pts.argsort()[::-1]

        topics = get_topics_info(indices, h5f, (15, 30))

    return render_template('topic/list.html', topics=topics)


@app.route('/documents_old')
def documents_old():
    with h5py.File('../data.hdf', mode='r') as h5f:
        nw = get_nwd(h5f).sum(0).A1
        indices = nw.argsort()[::-1]

        docs = get_docs_info(indices, h5f, (-1, 15))

    return render_template('document/list.html', docs=docs)


@app.route('/words')
def words():
    with h5py.File('../data.hdf', mode='r') as h5f:
        nw = get_nwd(h5f).sum(1).A1
        indices = nw.argsort()[::-1]

        words = get_words_info(indices, h5f, (15, 10))

    return render_template('word/list.html', words=words)


@app.route('/topic/<int:t>')
def topic(t):
    with h5py.File('../data.hdf', mode='r') as h5f:
        topic = get_topics_info([t], h5f)[0]

    return render_template('topic/single.html', topic=topic)


@app.route('/document/<slug>')
@app.route('/document/<int:d>')
@app.route('/document/<int:d>/<slug>')
def document(slug=None, d=None):
    with h5py.File('../data.hdf', mode='r') as h5f:
        if d is None:
            slugs = h5f['metadata']['slug']
            d = np.nonzero(slugs == slug)[0][0]

        doc = get_docs_info([d], h5f)[0]

        content = h5f['documents'][str(d)][...]

        filename = h5f['metadata']['filename', d]
        with codecs.open('static/docsdata/%s.html' % filename, encoding='utf-8') as f:
            html = f.read()

        topics_used = Counter()
        html_new = ''
        html_pos = 0
        for w, start, end, _, _, pts_glob in content:
            topic = doc.topics[pts_glob.argmax()]
            topics_used[topic.t] += 1

            html_new += html[html_pos:start]
            html_new += '<span data-word="%d" data-color="%d"><a href="#">' % (w, topic.t)
            html_new += html[start:end]
            html_new += '</a></span>'
            html_pos = end
        html_new += html[end:]

        html = html_new
        html = re.search(r'</header>(.*)</body>', html, re.DOTALL).group(1)

        html = re.sub(r'<img class="(\w+)" src="\w+/(eqn\d+).png".*?/>',
                      r'<span class="sprite-\2"></span>',
                      html, flags=re.DOTALL | re.MULTILINE)

        topics_in_content = [TopicTuple(t.t, (t.np, topics_used[t.t]))
                             for t in doc.topics
                             if t.t in topics_used]
        doc.topics = [t
                      for t in doc.topics
                      if t.t in topics_used]

        # generate smooth topics flow
        topics_flow = content['pts_glob']
        if topics_flow.ndim == 1:
            topics_flow = topics_flow[:, np.newaxis]
        wlen = 100
        window = np.bartlett(wlen)
        topics_flow = convolve1d(topics_flow, window / window.sum(), axis=0)
        topics_flow = list( starmap(TopicTuple, zip( [t.t for t in doc.topics], zip(*map(tuple, topics_flow)) )) )

    return render_template('document/single.html',
                            doc=doc,
                            topics_flow=topics_flow,
                            html_content=html,
                            topics_in_content=topics_in_content,
                            filename=filename)


@app.route('/word/w/<int:w>')
@app.route('/word/<word>')
def word(w=None, word=None):
    with h5py.File('../data.hdf', mode='r') as h5f:
        if w is None:
            words = h5f['dictionary'][...]
            w = np.nonzero(words == word)[0][0]
        word = get_words_info([w], h5f)[0]

    return render_template('word/single.html', word=word)