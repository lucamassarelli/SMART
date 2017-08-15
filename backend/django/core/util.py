import random
import redis

from django.db.models import Count
from django.contrib.auth import get_user_model
from django.conf import settings
from core.models import (Project, Data, Queue, DataQueue, User,
                         AssignedData)


def create_user(username, password, email):
    '''
    Create a user with the given attributes.
    Create a user in Django's authentication model and
    link it to our own project user model.
    '''
    auth_user = get_user_model().objects.create(
        username=username,
        password=password,
        email=email)

    return User.objects.create(auth_user=auth_user)


def iter_sample(iterable, sample_len):
    '''
    Sample the given number of items from the given iterable without
    loading all objects in the iterable into memory.
    Based on this: https://stackoverflow.com/a/12583436/2612566
    '''
    results = []
    iterator = iter(iterable)

    # Fill in the first sample_len elements
    try:
        for _ in range(sample_len):
            results.append(next(iterator))
    except StopIteration:
        raise ValueError("Sample larger than population.")

    # Randomize their positions
    random.shuffle(results)

    # At a decreasing rate, replace random items
    for i, v in enumerate(iterator, sample_len):
        r = random.randint(0, i)
        if r < sample_len:
            results[r] = v

    return results


def init_redis_queues():
    '''
    Create a redis queue for each queue in the database and fill it with
    the data linked to the queue.
    '''
    # Use a pipeline to reduce back-and-forth with the server
    pipeline = settings.REDIS.pipeline(transaction=False)

    for queue in Queue.objects.all():
        data_ids = [d.pk for d in queue.data.all()]
        if len(data_ids) > 0:
            # We'll get an error if we try to lpush without any data
            pipeline.lpush(queue.pk, *data_ids)

    pipeline.execute()


def create_project(name):
    '''
    Create a project with the given name.
    '''
    return Project.objects.create(name=name)


def add_data(project, data):
    '''
    Add data to an existing project.  Data should be an array of strings.
    '''
    bulk_data = (Data(text=d, project=project) for d in data)
    Data.objects.bulk_create(bulk_data)


def add_queue(project, length, user=None):
    '''
    Add a queue of the given length to the given project.  If a user is provided,
    assign the queue to that user.

    Return the created queue.
    '''
    return Queue.objects.create(length=length, project=project, user=user)


def fill_queue(queue):
    '''
    Fill a queue with unlabeled, unassigned data randomly selected from
    the queue's project. The queue doesn't need to be empty.

    If there isn't enough data left to fill the queue, use all the
    data available.

    TODO: Extend to use a model to fill the queue, when one has been trained
    for the queue's project.
    '''
    current_queue_len = queue.data.count()

    data_filters = {
        'project': queue.project,
        'labelers': None,
        'queues': None
    }

    try:
        # TODO: This has concurrency issues -- if multiple queues are filled
        # at the same time, they'll both draw their sample from the same set
        # of data and may assign some of the same data objects.
        # Need to have the sampling and insert in the same query,
        # (INSERT INTO ... SELECT ...)
        # which apparently requires raw SQL.  May require SELECT FOR UPDATE
        # SKIP LOCKED (Postgres 9.5+).
        queue_data = iter_sample(Data.objects
                                 .filter(**data_filters)
                                 .iterator(), queue.length - current_queue_len)
    except ValueError:
        # There isn't enough data left to fill the queue, so assign all of it
        queue_data = Data.objects.filter(**data_filters)

    DataQueue.objects.bulk_create(
        (DataQueue(queue=queue, data=d) for d in queue_data))


def pop_queue(queue):
    '''
    Remove a datum from the given queue (in redis and the database)
    and return it.

    Returns None and does nothing if the queue is empty.
    '''
    # Redis first, since this op is guaranteed to be atomic
    data_id = settings.REDIS.rpop(queue.pk)

    if data_id is None:
        return None

    data_obj = Data.objects.filter(pk=data_id).first()
    x = DataQueue.objects.filter(data=data_obj, queue=queue).delete()

    return data_obj


def get_nonempty_queue(project, user=None):
    '''
    Return the first nonempty queue for the given project and
    (optionally) user.
    '''
    first_nonempty_queue = None

    # Only check for user queues if we were passed a user
    if user is not None:
        nonempty_user_queues = (project.queue_set
                                .filter(user=user)
                                .annotate(
                                    data_count=Count('data'))
                                .filter(data_count__gt=0))

        if len(nonempty_user_queues) > 0:
            first_nonempty_queue = nonempty_user_queues.first()

    # If we didn't find a user queue, check project queues
    if first_nonempty_queue is None:
        nonempty_queues = (project.queue_set
                           .filter(user=None)
                           .annotate(
                               data_count=Count('data'))
                           .filter(data_count__gt=0))

        if len(nonempty_queues) > 0:
            first_nonempty_queue = nonempty_queues.first()

    return first_nonempty_queue


def assign_datum(user, project):
    '''
    Given a user and project, figure out which queue to pull from;
    then pop a datum off that queue and assign it to the user.
    '''
    first_nonempty_queue = get_nonempty_queue(project, user=user)

    if first_nonempty_queue is None:
        return None
    else:
        datum = pop_queue(first_nonempty_queue)
        AssignedData.objects.create(data=datum, user=user,
                                    queue=first_nonempty_queue)
        return datum
