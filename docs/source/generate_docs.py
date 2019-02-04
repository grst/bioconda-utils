"""Bioconda-Utils sphinx extension

This module builds the documentation for our recipes
"""

import os
import os.path as op
from typing import Any, Dict, List, Tuple, Optional

from jinja2.sandbox import SandboxedEnvironment

from sphinx import addnodes
from docutils import nodes
from docutils.parsers import rst
from docutils.statemachine import StringList
from sphinx.domains import Domain, ObjType, Index
from sphinx.directives import ObjectDescription
from sphinx.environment import BuildEnvironment
from sphinx.roles import XRefRole
from sphinx.util import logging as sphinx_logging
from sphinx.util import status_iterator
from sphinx.util.nodes import make_refnode
from sphinx.util.parallel import ParallelTasks, parallel_available, make_chunks
from sphinx.util.rst import escape as rst_escape
from sphinx.util.osutil import ensuredir
from sphinx.jinja2glue import BuiltinTemplateLoader

from conda.exports import VersionOrder

from bioconda_utils.utils import RepoData, load_config

# Aquire a logger
try:
    logger = sphinx_logging.getLogger(__name__)  # pylint: disable=invalid-name
except AttributeError:  # not running within sphinx
    import logging
    logger = logging.getLogger(__name__)  # pylint: disable=invalid-name

try:
    from conda_build.metadata import MetaData
    from conda_build.exceptions import UnableToParse
except Exception:
    logging.exception("Failed to import MetaData")
    raise


BASE_DIR = op.dirname(op.abspath(__file__))
RECIPE_DIR = op.join(op.dirname(BASE_DIR), 'bioconda-recipes', 'recipes')
OUTPUT_DIR = op.join(BASE_DIR, 'recipes')
RECIPE_BASE_URL = 'https://github.com/bioconda/bioconda-recipes/tree/master/recipes/'


def as_extlink_filter(text):
    """Jinja2 filter converting identifier (list) to extlink format

    Args:
      text: may be string or list of strings

    >>> as_extlink_filter("biotools:abyss")
    "biotools: :biotool:`abyss`"

    >>> as_extlink_filter(["biotools:abyss", "doi:123"])
    "biotools: :biotool:`abyss`, doi: :doi:`123`"
    """
    def fmt(text):
        assert isinstance(text, str), "identifier has to be a string"
        text = text.split(":", 1)
        assert len(text) == 2, "identifier needs at least one colon"
        return "{0}: :{0}:`{1}`".format(*text)

    assert isinstance(text, list), "identifiers have to be given as list"

    return list(map(fmt, text))


def underline_filter(text):
    """Jinja2 filter adding =-underline to row of text

    >>> underline_filter("headline")
    "headline\n========"
    """
    return text + "\n" + "=" * len(text)


def rst_escape_filter(text):
    """Jinja2 filter escaping RST symbols in text

    >>> rst_excape_filter("running `cmd.sh`")
    "running \`cmd.sh\`"
    """
    if text:
        return rst_escape(text)
    return text


class Renderer:
    """Jinja2 template renderer

    - Loads and caches templates from paths configured in conf.py
    - Makes additional jinja filters available:
      - underline -- turn text into a RSt level 1 headline
      - escape -- escape RST special characters
      - as_extlink -- convert (list of) identifiers to extlink references
    """
    def __init__(self, app):
        template_loader = BuiltinTemplateLoader()
        template_loader.init(app.builder)
        template_env = SandboxedEnvironment(loader=template_loader)
        template_env.filters['rst_escape'] = escape_filter
        template_env.filters['underline'] = underline_filter
        template_env.filters['as_extlink'] = as_extlink_filter
        self.env = template_env
        self.templates: Dict[str, Any] = {}

    def render(self, template_name, context):
        """Render a template file to string

        Args:
          template_name: Name of template file
          context: dictionary to pass to jinja
        """
        try:
            template = self.templates[template_name]
        except KeyError:
            template = self.env.get_template(template_name)
            self.templates[template_name] = template

        return template.render(**context)

    def render_to_file(self, file_name, template_name, context):
        """Render a template file to a file

        Ensures that target directories exist and only writes
        the file if the content has changed.

        Args:
          file_name: Target file name
          template_name: Name of template file
          context: dictionary to pass to jinja

        Returns:
          True if a file was written
        """
        content = self.render(template_name, context)

        # skip if exists and unchanged:
        if os.path.exists(file_name):
            with open(file_name, encoding="utf-8") as filedes:
                if filedes.read() == content:
                    return False  # unchanged

        ensuredir(op.dirname(file_name))
        with open(file_name, "w", encoding="utf-8") as filedes:
            filedes.write(content)
        return True


class CondaObjectDescription(ObjectDescription):
    typename = "[UNKNOWN]"

    option_spec = {
        'arch': rst.directives.unchanged,
        'badges': rst.directives.unchanged,
        'replaces_section_title': rst.directives.flag
    }

    def handle_signature(self, sig: str, signode: addnodes.desc) -> str:
        """Transform signature into RST nodes"""
        signode += addnodes.desc_annotation(self.typename, self.typename + " ")
        signode += addnodes.desc_name(sig, sig)

        if 'badges' in self.options:
            badges = addnodes.desc_annotation()
            badges['classes'] += ['badges']
            content = StringList([self.options['badges']])
            self.state.nested_parse(content, 0, badges)
            signode += badges


        if 'replaces_section_title' in self.options:
            section = self.state.parent
            if isinstance(section, nodes.section):
                title = section[-1]
                if isinstance(title, nodes.title):
                    section.remove(title)
                else:
                    signode += self.state.document.reporter.warning(
                        "%s:%s:: must follow section directly to replace section title"
                        % (self.domain, self.objtype), line = self.lineno
                    )
            else:
                signode += self.state.document.reporter.warning(
                    "%s:%s:: must be in section to replace section title"
                    % (self.domain, self.objtype), line = self.lineno
                )

        return sig

    def add_target_and_index(self, name: str, sig: str,
                             signodes: addnodes.desc) -> None:
        """Add to index and to domain data"""
        target_name = "-".join((self.objtype, name))
        if target_name not in self.state.document.ids:
            signodes['names'].append(target_name)
            signodes['ids'].append(target_name)
            signodes['first'] = (not self.names)
            self.state.document.note_explicit_target(signodes)
            objects = self.env.domaindata[self.domain]['objects']
            key = (self.objtype, name)
            if key in objects:
                self.env.warn(
                    self.env.docname,
                    "Duplicate entry {} {} at {} (other in {})".format(
                        self.objtype, name, self.lineno,
                        self.env.doc2path(objects[key][0])))
            objects[key] = (self.env.docname, target_name)

        index_text = self.get_index_text(name)
        if index_text:
            self.indexnode['entries'].append(('single', index_text, target_name, '', None))

    def get_index_text(self, name: str) -> str:
        return "{} ({})".format(name, self.objtype)


class CondaRecipe(CondaObjectDescription):
    typename = "Recipe"


class CondaPackage(CondaObjectDescription):
    typename = "Package"


class RecipeIndex(Index):
    name = "recipe_index"
    localname = "Recipe Index"
    shortname = "Recipes"

    def generate(self, docnames: Optional[List[str]] = None):
        content = {}

        objects = sorted(self.domain.data['objects'].items())
        for (typ, name), (docname, labelid) in objects:
            if docnames and docname not in docnames:
                continue
            entries = content.setdefault(name[0].lower(), [])
            subtype = 0 # 1 has subentries, 2 is subentry
            entries.append((
                name, subtype, docname, labelid, 'extra', 'qualifier', 'description'
            ))

        collapse = True
        return sorted(content.items()), collapse


class CondaDomain(Domain):
    """Domain for Conda Packages"""
    name = "conda"
    label = "Conda"

    object_types = {
        # ObjType(name, *roles, **attrs)
        'recipe': ObjType('recipe', 'recipe'),
        'package': ObjType('package', 'package'),
    }
    directives = {
        'recipe': CondaRecipe,
        'package': CondaPackage,
    }
    roles = {
        'recipe': XRefRole(),
        'package': XRefRole(),
    }
    initial_data = {
        'objects': {},  #: (type, name) -> docname, labelid
    }
    indices = [
        RecipeIndex
    ]

    def clear_doc(self, docname: str):
        # docs copied from Domain class
        """Remove traces of a document in the domain-specific inventories."""
        if 'objects' not in self.data:
            return
        to_remove = [
            key for (key, (stored_docname, _)) in self.data['objects'].items()
            if docname == stored_docname
        ]
        for key  in to_remove:
            del self.data['objects'][key]

    def resolve_xref(self, env: BuildEnvironment, fromdocname: str,
                     builder, typ, target, node, contnode):
        # docs copied from Domain class
        """Resolve the pending_xref *node* with the given *typ* and *target*.

        This method should return a new node, to replace the xref node,
        containing the *contnode* which is the markup content of the
        cross-reference.

        If no resolution can be found, None can be returned; the xref node will
        then given to the :event:`missing-reference` event, and if that yields no
        resolution, replaced by *contnode*.

        The method can also raise :exc:`sphinx.environment.NoUri` to suppress
        the :event:`missing-reference` event being emitted.
        """
        for objtype in self.objtypes_for_role(typ):
            if (objtype, target) in self.data['objects']:
                return make_refnode(
                    builder, fromdocname,
                    self.data['objects'][objtype, target][0],
                    self.data['objects'][objtype, target][1],
                    contnode, target + ' ' + objtype)
        return None  # triggers missing-reference

    def get_objects(self):
        # docs copied from Domain class
        """Return an iterable of "object descriptions", which are tuples with
        five items:

        * `name`     -- fully qualified name
        * `dispname` -- name to display when searching/linking
        * `type`     -- object type, a key in ``self.object_types``
        * `docname`  -- the document where it is to be found
        * `anchor`   -- the anchor name for the object
        * `priority` -- how "important" the object is (determines placement
          in search results)

          - 1: default priority (placed before full-text matches)
          - 0: object is important (placed before default-priority objects)
          - 2: object is unimportant (placed after full-text matches)
          - -1: object should not show up in search at all
        """
        for (typ, name), (docname, ref) in self.data['objects'].items():
            dispname = "Recipe '{}'".format(name)
            yield name, dispname, typ, docname, ref, 1

    def merge_domaindata(self, docnames: List[str], otherdata: Dict) -> None:
        """Merge in data regarding *docnames* from a different domaindata
        inventory (coming from a subprocess in parallel builds).
        """
        for (typ, name), (docname, ref) in otherdata['objects'].items():
            if docname in docnames:
                self.data['objects'][typ, name] = (docname, ref)

def generate_readme(folder, repodata, renderer):
    """Generates README.rst for the recipe in folder

    Args:
      folder: Toplevel folder name in recipes directory
      repodata: RepoData object
      renderer: Renderer object

    Returns:
      List of template_options for each concurrent version for
      which meta.yaml files exist in the recipe folder and its
      subfolders
    """
    output_file = op.join(OUTPUT_DIR, folder, 'README.rst')

    # Select meta yaml
    meta_fname = op.join(RECIPE_DIR, folder, 'meta.yaml')
    if not op.exists(meta_fname):
        for item in os.listdir(op.join(RECIPE_DIR, folder)):
            dname = op.join(RECIPE_DIR, folder, item)
            if op.isdir(dname):
                fname = op.join(dname, 'meta.yaml')
                if op.exists(fname):
                    meta_fname = fname
                    break
        else:
            logger.error("No 'meta.yaml' found in %s", folder)
            return []
    meta_relpath = meta_fname[len(RECIPE_DIR):]

    # Read the meta.yaml file(s)
    try:
        metadata = MetaData(meta_fname)
    except UnableToParse:
        logger.error("Failed to parse recipe %s", meta_fname)
        return []

    name = metadata.name()
    versions_in_channel = repodata.get_versions(name)
    sorted_versions = sorted(versions_in_channel.keys(), key=VersionOrder, reverse=True)

    # Format the README
    template_options = {
        'name': name,
        'about': (metadata.get_section('about') or {}),
        'extra': (metadata.get_section('extra') or {}),
        'versions': sorted_versions,
        'recipe_path': meta_relpath,
        'Package': '<a href="recipes/{0}/README.html">{0}</a>'.format(folder)
        'gh_recipes': RECIPE_BASE_URL,
    }

    renderer.render_to_file(output_file, 'readme.rst_t', template_options)

    versions = []
    for version in sorted_versions:
        version_info = versions_in_channel[version]
        version_tpl = template_options.copy()
        version_tpl.update({
            'Linux': '<i class="fa fa-linux"></i>' if 'linux' in version_info else '',
            'OSX': '<i class="fa fa-apple"></i>' if 'osx' in version_info else '',
            'Version': version
        })
        versions.append(version_tpl)
    return versions


def generate_recipes(app):
    """
    Go through every folder in the `bioconda-recipes/recipes` dir,
    have a README.rst file generated and generate a recipes.rst from
    the collected data.
    """
    renderer = Renderer(app)
    load_config(os.path.join(os.path.dirname(RECIPE_DIR), "config.yml"))
    repodata = RepoData()
    repodata.set_cache(op.join(app.env.doctreedir, 'RepoDataCache.csv'))
    # force loading repodata to avoid duplicate loads from threads
    repodata.df  # pylint: disable=pointless-statement
    recipes: List[Dict[str, Any]] = []
    recipe_dirs = os.listdir(RECIPE_DIR)

    if parallel_available and len(recipe_dirs) > 5:
        nproc = app.parallel
    else:
        nproc = 1

    if nproc == 1:
        for folder in status_iterator(
                recipe_dirs,
                'Generating package READMEs...',
                "purple", len(recipe_dirs), app.verbosity):
            if not op.isdir(op.join(RECIPE_DIR, folder)):
                logger.error("Item '%s' in recipes folder is not a folder",
                             folder)
                continue
            recipes.extend(generate_readme(folder, repodata, renderer))
    else:
        tasks = ParallelTasks(nproc)
        chunks = make_chunks(recipe_dirs, nproc)

        def process_chunk(chunk):
            _recipes: List[Dict[str, Any]] = []
            for folder in chunk:
                if not op.isdir(op.join(RECIPE_DIR, folder)):
                    logger.error("Item '%s' in recipes folder is not a folder",
                                 folder)
                    continue
                _recipes.extend(generate_readme(folder, repodata, renderer))
            return _recipes

        def merge_chunk(_chunk, res):
            recipes.extend(res)

        for chunk in status_iterator(
                chunks,
                'Generating package READMEs with {} threads...'.format(nproc),
                "purple", len(chunks), app.verbosity):
            tasks.add_task(process_chunk, chunk, merge_chunk)
        logger.info("waiting for workers...")
        tasks.join()


def setup(app):
    """Set up sphinx extension"""
    app.add_domain(CondaDomain)
    app.connect('builder-inited', generate_recipes)
    return {
        'version': "0.0.0",
        'parallel_read_safe': True,
        'parallel_write_safe': True
    }
