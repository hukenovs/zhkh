from .etalonuk import EtalonParser
from .moek import MoekParser
from .mosenergosbyt import MosenergosbytParser
from .mosvodokanal import MosvodokanalParser

PARSERS = {
    MoekParser.name: MoekParser,
    MosvodokanalParser.name: MosvodokanalParser,
    MosenergosbytParser.name: MosenergosbytParser,
    EtalonParser.name: EtalonParser,
}
