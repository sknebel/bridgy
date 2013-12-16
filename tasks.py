"""Task queue handlers.

TODO: cron job to find sources without seed poll tasks.
TODO: think about how to determine stopping point. can all sources return
comments in strict descending timestamp order? can we require/generate
monotonically increasing comment ids for all sources?
TODO: check HRD consistency guarantees and change as needed
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import json
import logging
import urlparse

# need to import model class definitions since poll creates and saves entities.
import facebook
import googleplus
import instagram
import models
import twitter
import util
from webmentiontools import send

from google.appengine.ext import db
from google.appengine.api import taskqueue
import webapp2

import appengine_config

# Known domains that don't support webmentions. Mainly just the silos.
WEBMENTION_BLACKLIST = (
  'amzn.com',
  'amazon.com',
  'facebook.com',
  'm.facebook.com',
  'instagram.com',
  'plus.google.com',
  'twitter.com',
  # these come from the text of tweets. we also pull the expended URL
  # from the tweet entities, so ignore these instead of resolving them.
  't.co',
  'youtube.com',
  'youtu.be',
  '', None,
  )

# allows injecting timestamps in task_test.py
now_fn = datetime.datetime.now


class Poll(webapp2.RequestHandler):
  """Task handler that fetches and processes new comments from a single source.

  Request parameters:
    source_key: string key of source entity
    last_polled: timestamp, YYYY-MM-DD-HH-MM-SS

  Inserts a propagate task for each comment that hasn't been seen before.
  """

  TASK_COUNTDOWN = datetime.timedelta(minutes=15)

  def post(self):
    logging.debug('Params: %s', self.request.params)

    key = self.request.params['source_key']
    source = db.get(key)
    if not source:
      logging.warning('Source not found! Dropping task.')
      return

    last_polled = self.request.params['last_polled']
    if last_polled != source.last_polled.strftime(util.POLL_TASK_DATETIME_FORMAT):
      logging.warning('duplicate poll task! deferring to the other task.')
      return

    try:
      self.do_post(source)
    except models.DisableSource:
      # the user deauthorized the bridgy app, so disable this source.
      source.status = 'disabled'
      source.save()
      logging.error('Disabling source!')
      # let this task complete successfully so that it's not retried.
    except:
      source.status = 'error'
      source.save()
      raise

  def do_post(self, source):
    logging.info('Polling %s %s', source.label(), source.key().name())
    activities = source.get_activities(fetch_replies=True, count=20)
    logging.info('Found %d activities', len(activities))

    for activity in activities:
      # use original post discovery to find targets
      source.as_source.original_post_discovery(activity)
      targets = util.trim_nulls(
        [t.get('url') for t in activity['object'].get('tags', [])
         if t.get('objectType') == 'article'])
      logging.info('Discovered original post URLs: %s', targets)

      # remove replies from activity JSON so we don't store them all in every
      # Comment entity.
      replies = activity['object'].pop('replies', {}).get('items', [])
      logging.info('Found %d comments for activity %s', len(replies),
                   activity.get('url'))

      for reply in replies:
        models.Comment(key_name=reply['id'],
                       source=source,
                       activity_json=json.dumps(activity),
                       comment_json=json.dumps(reply),
                       unsent=targets,
                       ).get_or_save()

    source.last_polled = now_fn()
    source.status = 'enabled'
    util.add_poll_task(source, countdown=self.TASK_COUNTDOWN.seconds)
    source.save()


class Propagate(webapp2.RequestHandler):
  """Task handler that sends a webmention for a single comment.

  Request parameters:
    comment_key: string key of comment entity
  """

  # request deadline (10m) plus some padding
  LEASE_LENGTH = datetime.timedelta(minutes=12)

  ERROR_HTTP_RETURN_CODE = 417  # Expectation Failed

  def post(self):
    logging.debug('Params: %s', self.request.params)

    try:
      comment = self.lease_comment()
    except:
      logging.exception('Could not lease comment')
      self.release_comment('new')
      raise

    if not comment:
      return

    try:
      _, comment_id = util.parse_tag_uri(comment.key().name())
      logging.info('Starting %s comment %s',
                   comment.source.kind(), comment.key().name())

      # generate local comment URL
      activity = json.loads(comment.activity_json)
      _, post_id = util.parse_tag_uri(activity['id'])
      local_comment_url = '%s/comment/%s/%s/%s/%s' % (
        self.request.host_url, comment.source.SHORT_NAME,
        comment.source.key().name(), post_id, comment_id)

      # send each webmention
      unsent = set(comment.unsent + comment.error)
      comment.error = []
      for target in unsent:
        # When debugging locally, redirect my (snarfed.org) webmentions to localhost
        if appengine_config.DEBUG and target.startswith('http://snarfed.org/'):
          target = target.replace('http://snarfed.org/', 'http://localhost/')

        domain = urlparse.urlparse(target).netloc
        if domain.startswith('www.'):
          domain = domain[4:]
        if domain in WEBMENTION_BLACKLIST:
          logging.info("Skipping %s ; we know %s doesn't support webmentions",
                       target, domain)
          comment.unsent.remove(target)
          continue

        # send! and handle response or error
        mention = send.WebmentionSend(local_comment_url, target)
        logging.info('Sending webmention from %s to %s', local_comment_url, target)
        if mention.send(timeout=999):
          logging.info('Sent! %s', mention.response)
          comment.sent.append(target)
        else:
          if mention.error['code'] == 'NO_ENDPOINT':
            logging.info('Giving up this target. %s', mention.error)
          else:
            self.fail('Error sending to endpoint: %s' % mention.error)
            comment.error.append(target)

        if target in comment.unsent:
          comment.unsent.remove(target)

      if comment.error:
        logging.error('Propagate task failed')
        self.release_comment(comment, 'error')
      else:
        self.complete_comment(comment)

    except:
      logging.exception('Propagate task failed')
      self.release_comment(comment, 'error')
      raise

  @db.transactional
  def lease_comment(self):
    """Attempts to acquire and lease the comment entity.

    Returns the Comment on success, otherwise None.

    TODO: unify with complete_comment
    """
    comment = db.get(self.request.params['comment_key'])

    if comment is None:
      self.fail('no comment entity!')
    elif comment.status == 'complete':
      # let this response return 200 and finish
      logging.warning('duplicate task already propagated comment')
    elif comment.status == 'processing' and now_fn() < comment.leased_until:
      self.fail('duplicate task is currently processing!')
    else:
      assert comment.status in ('new', 'processing', 'error')
      comment.status = 'processing'
      comment.leased_until = now_fn() + self.LEASE_LENGTH
      comment.save()
      return comment

  @db.transactional
  def complete_comment(self, comment):
    """Attempts to mark the comment entity completed.

    Returns True on success, False otherwise.
    """
    existing = db.get(comment.key())
    if existing is None:
      self.fail('comment entity disappeared!')
    elif existing.status == 'complete':
      # let this response return 200 and finish
      logging.warning('comment stolen and finished. did my lease expire?')
      return False
    elif existing.status == 'new':
      self.fail('comment went backward from processing to new!')

    assert comment.status == 'processing'
    comment.status = 'complete'
    comment.save()
    return True

  @db.transactional
  def release_comment(self, comment, new_status):
    """Attempts to unlease the comment entity.
    """
    existing = db.get(comment.key())
    if existing and existing.status == 'processing':
      comment.status = new_status
      comment.leased_until = None
      comment.save()

  def fail(self, message):
    """Fills in an error response status code and message.
    """
    self.error(self.ERROR_HTTP_RETURN_CODE)
    logging.error(message)
    self.response.out.write(message)


application = webapp2.WSGIApplication([
    ('/_ah/queue/poll', Poll),
    ('/_ah/queue/propagate', Propagate),
    ], debug=appengine_config.DEBUG)
