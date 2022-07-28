import json
import os
import sqlite3
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session
from tqdm import tqdm

from ..config import config
from ..sources.model import (
    Author,
    Institution,
    Paper,
    Release,
    Topic,
    Venue,
    from_dict,
)
from ..tools import get_uuid_tag, is_canonical_uuid, tag_uuid
from . import schema as sch


class Database:
    DATABASE_SCRIPT_FILE = os.path.join(
        os.path.dirname(__file__), "database.sql"
    )

    def __init__(self, filename):
        self.engine = create_engine(f"sqlite:///{filename}")
        connection = sqlite3.connect(filename)
        cursor = connection.cursor()
        with open(self.DATABASE_SCRIPT_FILE) as script_file:
            cursor.executescript(script_file.read())
            connection.commit()
        self.session = None
        self.cache = {}
        with self:
            self.canonical = {
                entry.hashid: entry.canonical
                for entry, in self.session.execute(select(sch.CanonicalId))
            }

    def __enter__(self):
        self.session = Session(self.engine).__enter__()
        return self

    def __exit__(self, *args):
        self.session.commit()
        self.session.__exit__(*args)
        self.session = None

    def acquire(self, x):
        # The id can be "transient" or "canonical". If it is "transient" it is defined
        # by its content, so we only ever need to acquire it once. If it is "canonical"
        # then it may contain new information we need to acquire, so we do not use the
        # cache for that.
        hid = x.hashid()
        tag = get_uuid_tag(hid)
        if hid in self.canonical:
            assert tag == "transient"
            return self.canonical[hid] or hid
        if not hid or tag == "canonical" or hid not in self.cache:
            self.cache[hid] = self._acquire(x)
            if tag == "transient":
                hid_object = sch.CanonicalId(hashid=hid, canonical=None)
                self.session.add(hid_object)
        return self.cache[hid]

    def _acquire(self, x):
        match x:
            case Paper(
                title=title, abstract=abstract, citation_count=cc
            ) as paper:

                pp = sch.Paper(
                    paper_id=paper.hashid(),
                    title=title,
                    abstract=abstract,
                    citation_count=cc,
                )
                self.session.merge(pp)

                for i, paper_author in enumerate(paper.authors):
                    author = paper_author.author
                    author_id = self.acquire(author)
                    pa = sch.PaperAuthor(
                        paper_id=pp.paper_id,
                        author_id=author_id,
                        author_position=i,
                    )
                    self.session.merge(pa)

                    for affiliation in paper_author.affiliations:
                        institution_id = self.acquire(affiliation)
                        pai = sch.PaperAuthorInstitution(
                            paper_id=pp.paper_id,
                            author_id=author_id,
                            institution_id=institution_id,
                        )
                        self.session.merge(pai)

                for release in paper.releases:
                    release_id = self.acquire(release)
                    stmt = (
                        insert(sch.t_paper_release)
                        .values(paper_id=pp.paper_id, release_id=release_id)
                        .on_conflict_do_nothing()
                    )
                    self.session.execute(stmt)

                for topic in paper.topics:
                    topic_id = self.acquire(topic)
                    stmt = (
                        insert(sch.t_paper_topic)
                        .values(paper_id=pp.paper_id, topic_id=topic_id)
                        .on_conflict_do_nothing()
                    )
                    self.session.execute(stmt)

                for link in paper.links:
                    lnk = sch.PaperLink(
                        paper_id=pp.paper_id,
                        type=link.type,
                        link=link.link,
                    )
                    self.session.merge(lnk)

                for scraper in paper.scrapers:
                    psps = sch.PaperScraper(
                        paper_id=pp.paper_id, scraper=scraper
                    )
                    self.session.merge(psps)

                return pp.paper_id

            case Author(name=name) as author:
                aa = sch.Author(author_id=author.hashid(), name=name)
                self.session.merge(aa)

                for link in author.links:
                    lnk = sch.AuthorLink(
                        author_id=aa.author_id,
                        type=link.type,
                        link=link.link,
                    )
                    self.session.merge(lnk)

                for alias in author.aliases:
                    aal = sch.AuthorAlias(
                        author_id=aa.author_id,
                        alias=alias,
                    )
                    self.session.merge(aal)

                for role in author.roles:
                    rr = sch.AuthorInstitution(
                        author_id=aa.author_id,
                        institution_id=self.acquire(role.institution),
                        role=role.role,
                        start_date=role.start_date,
                        end_date=role.end_date,
                    )
                    self.session.merge(rr)

                return aa.author_id

            case Institution(name=name, category=category) as institution:
                inst = sch.Institution(
                    institution_id=institution.hashid(),
                    name=name,
                    category=category,
                )
                self.session.merge(inst)
                return inst.institution_id

            case Release(
                date=date,
                date_precision=date_precision,
                volume=volume,
                publisher=publisher,
            ) as release:
                venue_id = self.acquire(release.venue)
                rr = sch.Release(
                    release_id=release.hashid(),
                    date=date,
                    date_precision=date_precision,
                    volume=volume,
                    publisher=publisher,
                    venue_id=venue_id,
                )
                self.session.merge(rr)
                return rr.release_id

            case Topic(name=name) as topic:
                tt = sch.Topic(topic_id=topic.hashid(), topic=name)
                self.session.merge(tt)
                return tt.topic_id

            case Venue(type=vtype, name=name) as venue:
                vv = sch.Venue(venue_id=venue.hashid(), type=vtype, name=name)
                self.session.merge(vv)

                for link in venue.links:
                    lnk = sch.VenueLink(
                        venue_id=vv.venue_id,
                        type=link.type,
                        link=link.link,
                    )
                    self.session.merge(lnk)

                return vv.venue_id

            case _:
                raise TypeError(f"Cannot acquire: {type(x).__name__}")

    def import_all(self, xs: list[BaseModel], history_file=None):
        if not xs:
            return
        history_file = history_file or config.history_file
        xs = list(xs)
        with self:
            for x in tqdm(xs):
                self.acquire(x)
        with open(history_file, "a") as f:
            data = [x.tagged_json() + "\n" for x in xs]
            f.writelines(data)

    def _accumulate_history_files(self, x, before, after, results):
        match x:
            case str() as pth:
                return self._accumulate_history_files(
                    Path(pth), before, after, results
                )
            case Path() as pth:
                if pth.is_dir():
                    self._accumulate_history_files(
                        list(pth.iterdir()), before, after, results
                    )
                else:
                    results.append(pth)
            case [*paths]:
                paths = list(sorted(paths))
                if before:
                    paths = [x for x in paths if x.name[: len(before)] < before]
                if after:
                    paths = [x for x in paths if x.name[: len(after)] > after]
                for subpth in paths:
                    self._accumulate_history_files(
                        subpth, before, after, results
                    )
            case _:
                assert False

    def replay(self, history=None, before=None, after=None):
        history = history or config.history_root
        history_files = []
        self._accumulate_history_files(history, before, after, history_files)
        for history_file in history_files:
            print(f"Replaying {history_file}")
            with self:
                with open(history_file, "r") as f:
                    lines = f.readlines()
                    for l in tqdm(lines):
                        self.acquire(from_dict(json.loads(l)))

    def _filter_ids(self, ids, create_canonical):
        for x in ids:
            if is_canonical_uuid(UUID(hex=x).bytes):
                canonical = x
                break
        else:
            canonical = UUID(bytes=tag_uuid(uuid4().bytes, "canonical")).hex
            create_canonical(canonical, x)
        return canonical, [x for x in ids if x != canonical]

    def merge_papers(self, paper_ids):
        def create_canonical(canonical, model):
            stmt = f"""
            INSERT OR IGNORE INTO paper (paper_id, title, abstract, citation_count)
            SELECT '{canonical}', title, abstract, citation_count FROM paper WHERE paper_id = X'{model}'
            """
            self.session.execute(stmt)

        canonical, ids = self._filter_ids(paper_ids, create_canonical)

        conds = [f"paper_id = X'{pid}'" for pid in ids]
        conds = " OR ".join(conds)

        tables = {
            sch.PaperAuthor: "paper_id",
            sch.PaperLink: "paper_id",
            sch.PaperFlag: "paper_id",
            sch.PaperAuthorInstitution: "paper_id",
            sch.t_paper_release: "paper_id",
            sch.t_paper_topic: "paper_id",
            sch.PaperScraper: "paper_id",
        }

        for table, field in tables.items():
            table = getattr(table, "__table__", table)

            stmt = f"""
            UPDATE OR IGNORE {table}
            SET {field} = X'{canonical}'
            WHERE {conds}
            """
            self.session.execute(stmt)

        stmt = f"""
        DELETE FROM paper WHERE {conds}
        """
        self.session.execute(stmt)

    def merge_authors(self, author_ids):
        def create_canonical(canonical, model):
            stmt = f"""
            INSERT OR IGNORE INTO author (author_id, name)
            SELECT '{canonical}', name FROM author WHERE author_id = X'{model}'
            """
            self.session.execute(stmt)

        canonical, ids = self._filter_ids(author_ids, create_canonical)

        conds = [f"author_id = X'{aid}'" for aid in ids]
        conds = " OR ".join(conds)

        tables = {
            sch.PaperAuthor: "author_id",
            sch.AuthorLink: "author_id",
            sch.AuthorAlias: "author_id",
            sch.AuthorInstitution: "author_id",
        }

        for table, field in tables.items():
            table = table.__table__

            stmt = f"""
            UPDATE OR IGNORE {table}
            SET {field} = X'{canonical}'
            WHERE {conds}
            """
            self.session.execute(stmt)

        stmt = f"""
        DELETE FROM author WHERE {conds}
        """
        self.session.execute(stmt)
