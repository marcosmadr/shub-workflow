"""
Base script class for spiders crawl managers.
"""
import abc
import json
import logging
from argparse import Namespace
from typing import Optional, List, Tuple, Dict, NewType, cast, Generator

from bloom_filter import BloomFilter
from typing_extensions import TypedDict, NotRequired

from shub_workflow.script import JobKey, JobDict
from shub_workflow.base import WorkFlowManager
from shub_workflow.utils import hashstr


_LOG = logging.getLogger(__name__)
_LOG.setLevel(logging.INFO)


SpiderArgs = NewType("SpiderArgs", Dict[str, str])


class JobParams(TypedDict):
    units: NotRequired[int]
    tags: NotRequired[List[str]]
    job_settings: NotRequired[Dict[str, str]]
    project_id: Optional[int]


class FullJobParams(JobParams):
    spider: NotRequired[Optional[str]]


def get_spider_args_from_params(params: JobParams) -> SpiderArgs:
    spider_args = cast(SpiderArgs, params.copy())
    for key in tuple(JobParams.__annotations__):
        spider_args.pop(key, None)
    return spider_args


def get_jobseq(tags: List[str]) -> Tuple[int, int]:
    for tag in tags:
        if tag.startswith("JOBSEQ="):
            tag = tag.replace("JOBSEQ=", "")
            jobseq, *rep = tag.split(".r")
            rep.append("0")
            return int(jobseq), int(rep[0])
    return 0, 0


class CrawlManager(WorkFlowManager):
    """
    Schedules a single spider job. If loop mode is enabled, it will shutdown only after the scheduled spider
    finished. Close reason of the manager will be inherited from spider one.
    """

    spider: Optional[str] = None

    def __init__(self):
        super().__init__()
        self.__close_reason = None

        # running jobs represents the state of a crawl manager.
        # a dict job key : (spider, job_args_override)
        self._running_job_keys: Dict[JobKey, Tuple[str, JobParams]] = {}

    @property
    def description(self):
        return self.__doc__

    def add_argparser_options(self):
        super().add_argparser_options()
        if self.spider is None:
            self.argparser.add_argument("spider", help="Spider name")
        self.argparser.add_argument("--spider-args", help="Spider arguments dict in json format", default="{}")
        self.argparser.add_argument("--job-settings", help="Job settings dict in json format", default="{}")
        self.argparser.add_argument("--units", help="Set default number of ScrapyCloud units for each job", type=int)

    def parse_args(self) -> Namespace:
        args = super().parse_args()
        if self.spider is None:
            self.spider = args.spider
        return args

    def get_spider_args(self, override: JobParams) -> JobParams:
        spider_args = json.loads(self.args.spider_args)
        spider_args.update(override)
        return spider_args

    def get_job_settings(self, override: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        job_settings = json.loads(self.args.job_settings)
        if override:
            job_settings.update(override)
        return job_settings

    def schedule_spider_with_jobargs(
        self,
        job_args_override: JobParams,
        spider: Optional[str] = None,
    ) -> Optional[JobKey]:
        spider = spider or self.spider
        if spider is not None:
            spider_args = self.get_spider_args(job_args_override)
            job_settings_override = spider_args.get("job_settings", None)
            spider_args["job_settings"] = self.get_job_settings(job_settings_override)
            spider_args["units"] = spider_args.get("units", self.args.units)
            jobkey = super().schedule_spider(spider, **spider_args)
            if jobkey is not None:
                self._running_job_keys[jobkey] = spider, job_args_override
            return jobkey
        return None

    def schedule_spider(
        self,
        spider: Optional[str] = None,
        tags: Optional[List[str]] = None,
        units: Optional[int] = None,
        project_id: Optional[int] = None,
        **kwargs,
    ) -> Optional[JobKey]:
        job_args_override = kwargs.pop("job_args_override", None)
        if spider is None or job_args_override is not None:
            job_args_override = job_args_override or {}
            job_args_override.update(kwargs)
            return self.schedule_spider_with_jobargs(job_args_override, spider)
        return super().schedule_spider(spider, **kwargs)

    def check_running_jobs(self) -> Dict[JobKey, str]:
        outcomes = {}
        running_job_keys = list(self._running_job_keys)
        while running_job_keys:
            jobkey = running_job_keys.pop()
            if (outcome := self.is_finished(jobkey)) is not None:
                spider, job_args_override = self._running_job_keys.pop(jobkey)
                if outcome in self.failed_outcomes:
                    _LOG.warning(f"Job {jobkey} finished with outcome {outcome}.")
                    if job_args_override is not None:
                        job_args_override = job_args_override.copy()
                    self.bad_outcome_hook(spider, outcome, job_args_override, jobkey)
                outcomes[jobkey] = outcome
        _LOG.info(f"There are {len(self._running_job_keys)} jobs still running.")

        return outcomes

    def bad_outcome_hook(self, spider: str, outcome: str, job_args_override: JobParams, jobkey: JobKey):
        if self.__close_reason is None:
            self.__close_reason = outcome

    def workflow_loop(self) -> bool:
        outcomes = self.check_running_jobs()
        if outcomes:
            return False
        if not self._running_job_keys:
            self.schedule_spider()
        return True

    def resume_running_job_hook(self, job: JobDict):
        key = job["key"]
        job_args_override = cast(JobParams, job.get("spider_args", {}).copy())
        job_args_override["tags"] = job["tags"]
        self._running_job_keys[key] = job["spider"], job_args_override
        _LOG.info(f"added running job {key}")

    def on_close(self):
        job = self.get_job()
        if job:
            close_reason = self.__close_reason
            self.finish(close_reason=close_reason)


class PeriodicCrawlManager(CrawlManager):
    """
    Schedule a spider periodically, waiting for the previous job to finish before scheduling it again with same
    parameters. Don't forget to set loop mode.
    """

    def bad_outcome_hook(self, spider: str, outcome: str, job_args_override: JobParams, jobkey: JobKey):
        pass

    def workflow_loop(self) -> bool:
        self.check_running_jobs()
        if not self._running_job_keys:
            self.schedule_spider()
        return True

    def on_close(self):
        pass


class GeneratorCrawlManager(CrawlManager):
    """
    Schedule a spider periodically, each time with different parameters yielded by a generator, until stop.
    Number of simultaneos spider jobs will be limited by max running jobs (see WorkFlowManager).
    Instructions:
    - Override set_parameters_gen() method. It must implement a generator of dictionaries, each one being
      the spider arguments (argument name: argument value) passed to the spider on each successive job.
    - Don't forget to set loop mode and max jobs (which defaults to infinite).
    """

    MAX_TOTAL_JOBS = 1000000

    def __init__(self):
        super().__init__()
        self.__parameters_gen = self.set_parameters_gen()
        self.__additional_jobs: List[FullJobParams] = []
        self.__next_job_seq = 1
        self.__jobids = BloomFilter(max_elements=self.MAX_TOTAL_JOBS, error_rate=0.001)

    def bad_outcome_hook(self, spider: str, outcome: str, job_args_override: JobParams, jobkey: JobKey):
        pass

    def add_job(self, spider: str, job_args_override: JobParams):
        params = cast(FullJobParams, (job_args_override or {}).copy())
        params["spider"] = spider
        self.__additional_jobs.append(params)

    @abc.abstractmethod
    def set_parameters_gen(self) -> Generator[JobParams, None, None]:
        yield from ()

    def __add_jobseq_tag(self, params: FullJobParams):
        tags = params.setdefault("tags", [])
        jobseq_tag = None
        for tag in tags:
            if tag.startswith("JOBSEQ="):
                jobseq_tag = tag
                break
        if jobseq_tag is None:
            jobseq_tag = f"JOBSEQ={self.__next_job_seq:010d}"
            self.__next_job_seq += 1
        else:
            tags.remove(jobseq_tag)
            jobseq, repn = get_jobseq([jobseq_tag])
            jobseq_tag = f"JOBSEQ={jobseq:010d}.r{repn + 1}"
        tags.append(jobseq_tag)

    def workflow_loop(self) -> bool:
        self.check_running_jobs()
        while len(self._running_job_keys) < self.max_running_jobs:
            try:
                if self.__additional_jobs:
                    next_params = self.__additional_jobs.pop(0)
                else:
                    next_params = next(self.__parameters_gen)
                spider = next_params.pop("spider", self.spider)
                assert spider is not None, "No spider set."
                spider_args = get_spider_args_from_params(next_params)
                jobid = self.get_job_id({"spider": spider, "spider_args": spider_args})
                if jobid not in self.__jobids:
                    self.__add_jobseq_tag(next_params)
                    self.schedule_spider(spider=spider, job_args_override=next_params)
                    self.__jobids.add(jobid)
            except StopIteration:
                if self._running_job_keys:
                    break
                return False
        return True

    @staticmethod
    def get_job_id(job: JobDict) -> str:
        jdict = job.get("spider_args", {}).copy()
        jdict["spider"] = job["spider"]
        for k, v in jdict.items():
            jdict[k] = str(v)
        jid = json.dumps(jdict, sort_keys=True)
        return hashstr(jid)

    def resume_running_job_hook(self, job: JobDict):
        super().resume_running_job_hook(job)
        jobid = self.get_job_id(job)
        self.__jobids.add(jobid)
        self.__next_job_seq = max(self.__next_job_seq, get_jobseq(job["tags"])[0] + 1)

    def resume_finished_job_hook(self, job: JobDict):
        jobid = self.get_job_id(job)
        if jobid not in self.__jobids:
            self.__jobids.add(jobid)
        self.__next_job_seq = max(self.__next_job_seq, get_jobseq(job["tags"])[0] + 1)

    def resume_workflow(self):
        super().resume_workflow()
        _LOG.info(f"Next job sequence number: {self.__next_job_seq}")

    def on_close(self):
        pass
