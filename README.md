# PetName

This utility will generate "pet names", consisting of a random combination of an adverb, adjective, and an animal.  These are useful for unique hostnames or containers, for instance.

As such, PetName tries to follow the tenets of Zooko's triangle.  Names are:

 - Human meaningful
 - Decentralized
 - Secure

Furthermore, PetName words must:

 - Consist of only a-z, with no other characters, all lowercase
 - Consist of 4 syllables or less
 - Consist of 12 characters or less
 - Be recognized by dict(1) or ispell(1) as a word
 - Be common according to the SCOWL database
 - End in "-ly" if an adverb

A 1-word PetName consists of one random animal name.  A 2-word Petname consists of a random adjective and a random animal name.  A 3-word (or more than 3 word) PetName consists of random adverb(s) and an adjective and a name.  Beyond 3 words, adverbs are prepended indefinitely.

## Command Line Usage

Command line help:

    usage: petname [-w|--words INT] [-s|--separator STR] [-l|--letters INT] [-u|--ubuntu]

    optional arguments:
        -w|--words            number of words in the name, default is 2
        -l|--letters          maximum number of letters in each word, default is 6
        -s|--separator        string used to separate name words, default is '-'
        -d|--dir              directory containing adverbs.txt, adjectives.txt, names.txt, default is \fI/usr/share/petname/\fP
        -u|--ubuntu           generate ubuntu-style names, alliteration of first character of each word

## Command Line Examples

    $ petname
    vigorous-yak

    $ petname --words 1
    slug

    $ petname --words 3
    daintily-quaint-mallard

    $ petname --words 4
    frostily-kingly-easy-bluegill

    $ petname --separator ":"
    desirous:glowworm

    $ petname -u
    light-lemming

## Golang Examples
```golang
package main

import (
        "flag"
        "fmt"
        "github.com/dustinkirkland/golang-petname"
)

var (
        words = flag.Int("words", 2, "The number of words in the pet name")
        separator = flag.String("separator", "-", "The separator between words in the pet name")
)

func main() {
        flag.Parse()
        fmt.Println(petname.Generate(*words, *separator))
}
```

## Python Examples

See: https://pypi.golang.org/pypi/petname

    $ pip install petname
    $ sudo apt-get install golang-petname

```golang
import argparse
import petname

parser = argparse.ArgumentParser(description='Generate human readable random names')
parser.add_argument('-w', '--words', help='Number of words in name, default=2', default=2)
parser.add_argument('-s', '--separator', help='Separator between words, default="-"', default="-")
parser.options = parser.parse_args()

print petname.Generate(int(parser.options.words), parser.options.separator)
```

## Credits

This project is authored and maintained by Dustin Kirkland.

